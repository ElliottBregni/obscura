# Contributing to the browser extension

New engineer? Read this + [ARCHITECTURE.md](./ARCHITECTURE.md). That's
the two docs you need.

## Dev loop

### First time

```bash
# from the repo root
uv sync --extra dev
make ext-install                       # installs native-host manifest
# then in Chrome:
#   chrome://extensions → Developer mode → Load unpacked
#     → select packages/browser-extension/
#   copy the extension id shown — it should match .keys/EXTENSION_ID
```

If the id doesn't match, you have a stale manifest. Run `make
ext-install` again.

### Every time you change the host or launcher

```bash
obscura-browser reload                 # or: pkill -f obscura_native_host.py
# then reload the Obscura card on chrome://extensions
```

**Chrome keeps native host processes alive for the lifetime of the
extension.** Rewriting code has no effect until you kill the old
process. This is the #1 source of "why isn't my change showing up".

### Every time you change panel JS/HTML/CSS

Just reload the Obscura card on `chrome://extensions`. No host restart
needed — the SW and panel script get re-read.

### Every time you change obscura core (`obscura/…`)

Reload the host (changes to Python need a fresh process). The panel
itself doesn't need reloading.

---

## Before you open a PR

```bash
# from repo root

# 1. Lint / format
ruff check packages/browser-extension/ obscura/cli/ obscura/core/
ruff format --check packages/browser-extension/ obscura/cli/

# 2. Type check
pyright packages/browser-extension/native-host/

# 3. Python tests
uv run pytest tests/browser_extension/ -m browser -v

# 4. JS syntax check (Node 18+)
node --check packages/browser-extension/src/sidepanel/sidepanel.js
node --check packages/browser-extension/src/background.js

# 5. JS unit tests (fast)
cd packages/browser-extension && npm test

# 6. E2E smoke test (Puppeteer, ~5s) — catches broken manifests,
#    JS parse errors, missing element ids
cd packages/browser-extension && npm run test:e2e

# 7. (Optional) Full handshake E2E — requires native host installed +
#    a Chrome build that permits native messaging. Flaky on CI.
cd packages/browser-extension && OBSCURA_E2E_FULL=1 npm run test:e2e

# 8. Smoke test manually: reload the extension, send a message, send a /mode,
#    check logs
obscura-browser reload
obscura-browser logs -n 50
```

CI runs 1–5 automatically via `.github/workflows/browser-extension.yml`.

---

## How to …

### Add a slash command
Don't touch the extension. Add it to `obscura/cli/commands.py:COMMANDS`.
It'll appear in panel autocomplete on next host start.

### Add a browser tool (DOM op)
1. Add a `ToolSpec` to `native-host/browser_tools.py:TOOLS` with an
   `op` name matching the JS handler you're about to write.
2. Add a `case "op_name":` in `sidepanel.js:runBrowserOp`.
3. Add a test that stubs `chrome.scripting.executeScript` and asserts
   the op's request/response shape.

### Add a wire frame type
Required PR checklist:
- [ ] Added to the table in `ARCHITECTURE.md`
- [ ] Added to the docstring at the top of `obscura_native_host.py`
- [ ] Handled in `_main()` of the host (if ext→host)
- [ ] Handled in `port.onMessage` switch of `sidepanel.js` (if host→ext)
- [ ] Covered by at least one test

### Add a confirmation widget kind
Extend `_install_widget_broker()` in the host. The generic `widget` /
`widget-response` flow handles arbitrary action sets — you rarely need
a new frame type. If you do, follow the wire-frame checklist above.

### Change the stored transcript/settings schema
**Bump `STORAGE_VERSION` in `sidepanel.js` and add a migration.** Never
silently rewrite. If a teammate pulls your branch without a migration,
their panel state resets on their next session. See the migration
comment block in `sidepanel.js` for the pattern.

### Add a Python dependency
Edit `pyproject.toml`, then `uv lock`. The host imports lazily so the
dependency only needs to be in the active venv.

### Add a JS dependency
We ship the extension **dependency-free** by design (no build step,
loads unpacked directly). If you genuinely need one:
1. Propose in the PR description and get 2 approvals.
2. Commit to a bundler (Vite) for everything, not just your new dep.
3. Update `ARCHITECTURE.md` → "Non-goals" section.

---

## PR expectations

- **Title:** `[ext] short description`. Use `[ext][host]` for host
  changes, `[ext][panel]` for UI, `[ext][proto]` for wire-protocol.
- **Description:** what, why, how to test manually. Include a screenshot
  or video for UI changes.
- **Tests:** every new wire frame, protocol field, or DOM op gets a
  test. No exceptions.
- **Docs:** if you change behaviour a user will see, update the
  relevant README section. If you change architecture, update
  `ARCHITECTURE.md`.
- **Undercover mode:** no AI attribution in commits, PR bodies, or
  code comments. First person, human voice.
- **Size:** prefer <500 LOC net diff. Split bigger changes unless the
  feature is genuinely atomic.

## Review expectations

- Two approvals for changes to the wire protocol, session lifecycle,
  storage schema, or the monkey-patch installers.
- One approval for everything else.
- Reviewer owns: did the author update the table in ARCHITECTURE.md,
  the protocol docstring, and the test file? If not, block.

---

## Common pitfalls

1. **Forking obscura core.** If you find yourself adding a helper to
   the host that duplicates something in `obscura.cli.commands` or
   `obscura.cli.session`, stop. There's almost certainly an extension
   point you're missing. Read ARCHITECTURE.md §"Why a thin adapter
   matters" and ask in review.
2. **Forgetting to reload the host after editing the launcher.** You
   will do this. The `obscura-browser status` command shows which pid
   is running; compare to `ls -la` on the launcher to see if they
   match.
3. **Assuming the panel can reach the native host directly.** It
   can't. All host traffic goes through `background.js`. If you need
   the panel to have richer control, add a wire frame — don't try to
   smuggle native-messaging permissions into the panel.
4. **Writing to `~/.obscura/` without considering multi-profile.**
   Two Chrome profiles = two panels = one shared directory.
   Use `chrome.runtime.id`-scoped paths for per-profile state.
5. **Putting `extension.pem` anywhere inside `packages/browser-extension/`.**
   Chrome's unpacked loader scans the whole directory and warns when it
   finds a private key — if a pack script ever runs, the key ships.
   Canonical location is `packages/browser-extension-keys/extension.pem`
   (sibling, gitignored). Don't force-add it, don't paste it anywhere. If
   it leaks, regenerate immediately — the old id is forever unsafe.

---

## Who to ask

- **Wire protocol, session lifecycle, monkey-patches:** Elliott
- **obscura core (cli, core, providers):** read `AGENTS.md` at the
  repo root for the impact-analysis workflow before you refactor.
- **Panel UI / CSS:** the team. We're still figuring out conventions —
  propose, don't ask permission.

## Keyboard shortcuts for development

- `⌘+Shift+I` on the side panel — open panel DevTools
- `chrome://extensions` → *service worker* link under Obscura — SW
  console (logs from `background.js`)
- `chrome://extensions` → *Inspect views: offscreen* — if we ever add
  an offscreen doc
- `obscura-browser logs -f` — host log tail
- `obscura-browser status` — what's installed, what's running

Welcome aboard.
