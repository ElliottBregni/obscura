# Obscura Auth / Secrets / Profile — v1 + roadmap

_Last updated: 2026-04-24_

This document captures what shipped in the auth-and-secrets workstream
and what's queued next. It's the single reference for "where is this
system today and where is it going."

---

## What v1 is

Four layered systems, one coherent story: **per-user state follows you
across machines, secrets stay out of repos, plugins don't leak to
subprocesses.**

### 1. Layered secret resolver — `obscura/auth/secrets.py`

`resolve(name)` walks five tiers in order:

| # | Tier | Where it comes from |
|---|------|---------------------|
| 1 | **shell env** | Snapshotted at module import, before any dotenv. What the operator exported in their shell / Docker passed via `-e`. |
| 2 | **OS keyring** | macOS Keychain, Windows Credential Manager, Linux Secret Service. |
| 3 | **Supabase cloud vault** | User-scoped encrypted bag in `user_metadata`. Regular entries = email-derived key; `--risk` entries = passphrase-derived key. |
| 4 | **dotenv** | `.env` files (CWD, `~/.obscura/.env`, package root). Sits *below* keyring so repo `.env`s can't shadow keychain values. |
| 5 | **default** | Caller-provided fallback. |

Supporting machinery:

- `sources()` reports which tier any given name resolves from (`"shell"` / `"keyring"` / `"supabase"` / `"dotenv"` / `"missing"`).
- `materialize_to_environ()` runs at CLI bootstrap, copies keyring + cloud-vault values into `os.environ` so plugins reading `os.environ` directly get the same precedence.
- `safe_subprocess_env()` — subprocess env filter. Default behaviour is `os.environ` passthrough; with `OBSCURA_TOOL_ENV_STRICT=1` it strips `KNOWN_SECRET_NAMES` + materialised keys before every tool spawn (`run_shell`, `run_python3`, `code_sandbox`, MCP stdio, `BackgroundTaskManager.start`, worktree `git`). Every strip event lands in a JSONL audit log.
- `store()` validates values (rejects NUL bytes, caps at 64 KB).

CLI surface (`obscura-auth secrets …`):

```
secrets list [--only-set]                    # env / keyring / supabase / dotenv / missing
secrets get  NAME [--reveal]                 # masked by default
secrets set  NAME VALUE [--force]            # write to keyring
secrets delete NAME [--force]
secrets export [--shell bash|fish]           # shell-eval-able export lines
secrets strict-env [--tail N] [--clear]      # show status + tail audit log
```

The resolver is used by the LLM-backend auth chain in `obscura/core/auth.py` — the Copilot/Claude/OpenAI/Moonshot key lookups route through `_secrets.resolve()` so anything stored in any tier is visible to the backend.

### 2. Encrypted cloud vaults — `obscura/auth/supabase_secrets.py`

Two parallel bags inside `user_metadata`, each with homogeneous encryption:

| Vault | Key source | When to use |
|---|---|---|
| `obscura_vault` | scrypt(user's email, salt) → Fernet key | Default. Zero-friction: log in, it works. |
| `obscura_vault_risk` | scrypt(passphrase, separate salt) → Fernet key | `cloud push NAME --risk`. Defense against anyone who grabs a raw Supabase JWT. |

Per-user salts live in `obscura_vault_salt` / `obscura_vault_risk_salt`. Derived Fernet keys are cached in the OS keyring (`obscura-cli / cloud-passphrase-key` for the risky side). Passphrase itself is never stored — user commits to remembering it or keeping it in a password manager.

CLI surface (`obscura-auth secrets cloud …`):

```
cloud status                                 # names with [risk] marker
cloud push NAME [--risk] [--yes]             # confirms interactively by default
cloud pull NAME [--print]                    # prompts for passphrase if needed
cloud pull-all                               # fresh-machine bulk; prompts once
cloud delete NAME [--yes]
cloud passphrase set | clear                 # manage the risky-vault key
```

Hard-blocks: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_JWT_SECRET`, `SUPABASE_SERVICE_ROLE_KEY` are refused at the client level — can't be cloud-stored.

### 3. User profile — `obscura/auth/profile.py`

Plaintext non-secret context in `user_metadata.obscura_profile`. Typed via Pydantic (`ObscuraProfile`, `DeviceInfo`), `extra="forbid"` so schema drift surfaces on read.

Fields:

- **Identity:** `display_name`, `timezone`
- **Backend defaults:** `default_backend`, `default_model`
- **Behaviour toggles:** `undercover`, `feature_flags`
- **Continuity:** `last_workspace`, `last_session_id`, `last_cwd`
- **Devices:** `[DeviceInfo]` — UUID per machine, name/platform/hostname/first_seen/last_seen

Per-machine UUIDs live at `~/.obscura/machine.id` (0600 perms). Registering a device copies that UUID plus harmless context into the cloud list.

CLI surface (`obscura-auth profile …`):

```
profile show
profile get FIELD
profile set FIELD VALUE                      # bools accept true/false/yes/no; lists accept csv
profile unset FIELD

profile device list
profile device current                       # prints machine.id + entry if registered
profile device register [--name L]
profile device rename NEW_NAME
profile device remove ID [--yes]
profile device touch                         # bump last_seen
```

### 4. Supporting infrastructure

- **Subprocess env strict mode** — `OBSCURA_TOOL_ENV_STRICT=1` with audit log at `~/.obscura/logs/secrets-audit.jsonl`.
- **Shell env snapshot** captured at module import prevents CWD `.env` files from shadowing what the operator deliberately exported.
- **Distribution strategy** ([memory](../../.claude/projects/-Users-elliottbregni-dev-obscura/memory/project_obscura_distribution.md)): install is `uv tool install --editable .` only. PyPI / Homebrew / Windows installers deferred.

### Tests

- `tests/unit/obscura/auth/test_secrets.py` — resolver tier precedence, `sources()`, `materialize_to_environ()`, `safe_subprocess_env()`, strict-mode audit log
- `tests/unit/obscura/auth/test_supabase_secrets.py` — crypto primitives, regular + risky vault CRUD, never-push guard, passphrase caching
- `tests/unit/obscura/auth/test_profile.py` — machine ID persistence, Pydantic models, device register/rename/remove
- `tests/unit/obscura/cli/test_auth_commands.py` — every CLI subcommand
- **Totals:** 363 tests passing across the auth / secrets / profile surface.

---

## Next up

Listed roughly in order of decreasing ROI for the current solo-dev stage.

### Near-term (weeks, not months)

1. **Profile ↔ backend selection integration.** Wire `profile.default_backend` / `default_model` into `cli/__init__.py` so `obscura` with no flags picks the user's preferred backend. Precedence: CLI flag > `OBSCURA_BACKEND` env > profile default > hardcoded `copilot`.
2. **Auto-register current device on CLI bootstrap.** Opt-in via `OBSCURA_PROFILE_AUTO_REGISTER=1`, bumps `last_seen` each run. Without the flag, explicit `profile device register` is required.
3. **Passphrase rotation.** `cloud passphrase rotate` — pulls all risky entries into memory, clears the cached key, prompts for new passphrase, re-pushes under the new key. Today's workaround: pull-all → clear → set new → re-push individually.
4. **Shell completion generation.** `obscura-auth completion bash|zsh|fish` — produce a source-able completion script. The `secrets export` command already models the two-shell approach.

### Medium-term (when they're needed)

1. **Keeper / 1Password integration for the risky passphrase.** Today users remember their passphrase or keep it in any password manager. A helper that reads the passphrase from Keeper CLI / 1Password CLI on first use removes the "what was that passphrase again" moment.
2. **Resolver caching with TTL.** Cloud vault snapshots cache for process lifetime. Long-running agents (Kairos daemons) could benefit from refreshing every N minutes so rotated keys propagate without restart.
3. **Team-shared vaults.** A third vault field (`obscura_vault_team_$ID`) encrypted with a symmetric key shared out-of-band. Meaningful only once Obscura is running for more than one person.
4. **`/secrets` slash-command coverage for cloud and profile.** The in-REPL `/secrets` supports `list/get/set/delete`; cloud + profile are CLI-only today. Trivial to mirror once the CLI surface is stable.

### Long-term / explicit non-goals for now

- **Audit dashboard.** The JSONL audit log at `~/.obscura/logs/secrets-audit.jsonl` already captures strict-env strips + cloud push/delete events. Building a web UI for it is multi-user territory — explicitly deferred.
- **Cross-platform distribution story.** Deferred per the distribution-strategy memory. `uv tool install --editable .` is the install path until someone other than the primary dev needs to run Obscura.
- **Server-side-only vault.** The current design is user-scoped (user JWT drives reads/writes). A server-role vault would need admin-level Supabase auth baked into the client, which contradicts the "no service-role key on laptops" principle.

---

## Known trade-offs baked into v1

Things that are intentional today, worth re-evaluating later:

- **Email-derived vault key is obfuscation-grade.** Anyone with the Supabase JWT can read the email and re-derive. The real protection is Supabase row auth; the crypto layer is defense against backups, admin-UI glances, and lazy inspection.
- **Losing the risky passphrase = losing the risky vault.** No recovery path in v1. When Keeper integration lands (Medium-term #1), Keeper becomes the recovery channel.
- **Subprocess strict mode is opt-in.** Default behaviour still inherits full `os.environ` into tool subprocesses. Flipping the default to strict would break anyone whose MCP / plugin / shell tool relied on picking up a key from env — deferred until someone actively asks for it.
- **Profile resolution is one-way.** The profile is fetched + cached on first use. Writing to `profile.last_workspace` from inside the REPL is possible but not wired — it'd need a hook at session start / end.
- **No local plaintext mirror of cloud entries.** When offline on a fresh machine, the cloud vault is unreachable, and the keyring is empty. `pull-all` solves this once per machine but there's no sync daemon. By design — a sync daemon is the kind of thing you add when it starts to hurt.

---

## File map

```
obscura/auth/
  secrets.py           # resolver, sources, materialize, safe_subprocess_env
  supabase_secrets.py  # encrypted cloud vaults (regular + risky)
  profile.py           # user_metadata.obscura_profile + devices
  supabase.py          # JWT verifier for server-side Supabase auth (unchanged)

obscura/cli/
  auth_commands.py     # all `obscura-auth …` subcommands

tests/unit/obscura/auth/
  test_secrets.py
  test_supabase_secrets.py
  test_profile.py

tests/unit/obscura/cli/
  test_auth_commands.py
```
