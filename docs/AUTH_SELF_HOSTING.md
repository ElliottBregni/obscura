# Self-hosting Obscura Auth

By default, the Obscura README points users at [auth.modernized-ai.com](https://auth.modernized-ai.com) for sign-in — that's a hosted instance backed by a Modernized AI Supabase project.

If you're running your own Obscura deployment, you'll want to point it at your own Supabase (so you control the user pool, roles, and tokens) or use long-lived API keys for headless callers. This doc covers both.

---

## Run Obscura against your own Supabase

One-time setup when standing up a new deployment.

### 1. Create a Supabase project

- Sign up at [supabase.com](https://supabase.com) and create a new project (free tier is fine to start)
- Once it provisions, you'll need three things from **Project Settings → API**:
  - `Project URL` (looks like `https://<project-ref>.supabase.co`)
  - `anon public` key
  - The JWKS URL: `https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json`

### 2. Create a GitHub OAuth App

Go to [github.com/settings/developers](https://github.com/settings/developers) → **New OAuth App**.

| Field | Value |
|-------|-------|
| Application name | Obscura (or your deployment name) |
| Homepage URL | `https://your-domain` (or `http://localhost:5173` for dev) |
| Authorization callback URL | `https://<project-ref>.supabase.co/auth/v1/callback` |

Copy the **Client ID** and generate a **Client Secret** — you'll paste both into Supabase next.

### 3. Enable GitHub auth in Supabase

In your Supabase dashboard: **Authentication → Providers → GitHub**.

- Toggle GitHub on
- Paste the Client ID and Client Secret from step 2
- Save

### 4. Point Obscura at your Supabase project

In your Obscura `.env`:

```bash
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_ANON_KEY=<anon public key from step 1>
SUPABASE_JWKS_URL=https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json
```

That's it — restart the API, and `/api/*`, `/mcp/*`, `/a2a/*` will now validate tokens against your Supabase project.

Auth is always enforced on those routes; there is no off-switch.

### How token validation works

Obscura validates Supabase bearer tokens by fetching the JWKS on first use — no shared secret to paste or rotate. If your project is still on legacy HS256, either:

- Migrate to asymmetric keys via Supabase Dashboard → **Project Settings → JWT Signing Keys → Rotate**, or
- Set `SUPABASE_JWT_SECRET` instead of `SUPABASE_JWKS_URL`.

### Rotating the GitHub OAuth secret

1. Generate a new Client Secret in your GitHub OAuth App
2. Paste it into the Supabase dashboard (Authentication → Providers → GitHub)
3. Save
4. Revoke the old secret on GitHub

No Obscura restart or code change needed — Supabase handles the OAuth handshake.

### Roles

User roles are stored on the Supabase user record at `app_metadata.roles`. Set them from the Supabase dashboard: **Authentication → Users → \<your user\> → Edit `app_metadata`**.

Valid roles: `admin`, `operator`, `agent:read`, `agent:claude`, `agent:copilot`, etc. — see `obscura/auth/models.py` for the full list.

New GitHub sign-ups default to `agent:read`. Promote selectively.

---

## API keys (CI / scripts / MCP clients)

Headless callers that can't complete the OAuth flow use long-lived API keys instead. Set them via the `OBSCURA_API_KEYS` environment variable on the Obscura server:

```bash
OBSCURA_API_KEYS="mykey:service-account:agent:read,agent:copilot"
```

Then call the API with the key as a bearer token:

```bash
curl -H "Authorization: Bearer mykey" http://localhost:8080/api/v1/whoami
```

### Format

```
token:user_id:role1,role2[;token:user_id:role1,role2;...]
```

- `token` — the secret string the client sends as `Authorization: Bearer <token>`
- `user_id` — arbitrary identifier shown in audit logs
- `role1,role2` — comma-separated roles (same vocabulary as Supabase `app_metadata.roles`)
- Separate multiple keys with `;`

### Operational notes

- Treat API keys like passwords. They grant the listed roles to anyone holding them.
- Audit-log every call — see `~/.obscura/logs/deep.jsonl`.
- Rotate by adding the new key, switching clients over, then removing the old one.
- For MCP clients, the key goes in the `env` block of your MCP server config (see the main [README](../README.md#mcp-server-claude-code--claude-desktop) for the Claude Desktop example).

---

## Choosing between OAuth and API keys

| Use case | Recommendation |
|----------|----------------|
| Human users in a browser | OAuth (Sign in with GitHub) |
| Local CLI scripts | OAuth token from DevTools, copied once |
| CI / GitHub Actions | API key in repo secrets |
| MCP server config | API key (no browser available) |
| Long-running daemons | API key |
| One-off curl from a terminal | API key |

The OAuth path is preferred where it's practical because tokens auto-refresh and revocation flows through Supabase. API keys are for cases where the client genuinely can't sit in front of a browser.
