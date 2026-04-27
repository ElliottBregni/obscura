# flags/

Feature flag definitions for sync to Unleash (and future adapters).

One TOML file per flag. Filename must match the `name` field inside the file.

## Format

```toml
# flags/dark-mode.toml
name = "dark-mode"
description = "Enable dark mode UI for all users"
type = "release"
enabled = true

[[strategies]]
name = "default"
```

## Flag types

Unleash supports: `release`, `experiment`, `operational`, `kill-switch`, `permission`.

## Workflow

```bash
# Edit or create a flag file, then push:
vault-gen sync push ${name} --adapter unleash

# See what would change before pushing:
vault-gen sync diff ${name} --adapter unleash

# Pull live Unleash state into the repo:
vault-gen sync pull ${name} --adapter unleash
```

## Notes

- Flag files are the source of truth when pushing.
- Flags in Unleash but not in `flags/` will be **archived** on push.
- Run `vault-gen sync pull` to seed this directory from an existing Unleash project.
- Requires `VAULT_GEN_UNLEASH_TOKEN` env var with admin API permissions.
- Enable syncing in `sync.toml` at the repo root.
