# obscura — browser extension

A Chrome MV3 side-panel that talks to a local `obscura` process over a
**native messaging host**. No HTTP server needed.

```
  ┌───────────────────────────┐
  │  Side panel (HTML / JS)   │  chrome.runtime.Port
  └──────────┬────────────────┘
             │
  ┌──────────▼────────────────┐
  │  Service worker (bridge)  │  chrome.runtime.connectNative
  └──────────┬────────────────┘
             │  stdin/stdout — 4-byte LE length + JSON
  ┌──────────▼────────────────┐
  │  obscura_native_host.py   │  ObscuraClient.stream(...)
  └───────────────────────────┘
```

## Install (macOS / Linux)

1. **Load the extension unpacked.** In Chrome open `chrome://extensions`,
   toggle *Developer mode*, click *Load unpacked*, and select this
   `packages/browser-extension/` directory.

2. **Copy the extension id.** It appears under the loaded card on
   `chrome://extensions`. It looks like `abcdefghijklmnopabcdefghijklmnop`.

3. **Install the native host manifest.** From the repo root:

   ```bash
   cd packages/browser-extension/native-host
   ./install.sh <extension-id>
   ```

   The installer:
   - Writes `com.obscura.host.json` into every Chrome-family
     `NativeMessagingHosts/` directory it finds on your machine
     (Chrome, Chromium, Brave, Edge, Arc, Vivaldi).
   - Generates a small launcher shell script that invokes
     `obscura_native_host.py` with `uv run python` by default. Override
     with `OBSCURA_PYTHON=/path/to/python ./install.sh <id>`.

4. **Reload the extension** (`chrome://extensions` → reload icon) so Chrome
   re-reads the manifest.

5. **Open the side panel** — click the Obscura toolbar icon, or use the
   side-panel launcher in Chrome.

## Usage

- Pick a backend from the dropdown (`copilot` by default).
- Leave `ctx` checked to attach the current tab's URL, title, and selected
  text to every prompt.
- `⌘/Ctrl + Enter` sends. `new` starts a fresh session.
- Conversation id is carried across turns so the backend sees continuous
  context until you hit `new`.

## Wire protocol

See `native-host/obscura_native_host.py` docstring for the full protocol.
Summary:

| direction    | type     | payload                                                              |
|--------------|----------|----------------------------------------------------------------------|
| ext → host   | `send`   | `{id, prompt, backend, model?, session_id?, context?}`               |
| host → ext   | `ready`  | `{version}` — sent once on connect                                   |
| host → ext   | `chunk`  | `{id, text}` — streaming delta                                       |
| host → ext   | `done`   | `{id, session_id?}` — end of turn                                    |
| host → ext   | `error`  | `{id?, message, trace?}`                                             |

## Changing the launcher / python

Chrome keeps the native host process alive across messages and across
side-panel opens — it only respawns when the service worker disconnects.
So after rerunning `install.sh` (or hand-editing `obscura-native-host`):

```bash
pkill -f obscura_native_host.py      # kill the running host
# then on chrome://extensions click the reload icon on the Obscura card
```

Otherwise you'll keep talking to a stale host that was spawned with the old
python.

## Debugging

Host log:

```bash
tail -f ~/.obscura/logs/browser-extension-host.log
```

Extension background console: `chrome://extensions` → *service worker*
link under the Obscura card.

When the native host crashes at launch, Chrome surfaces stderr in that
service-worker console (truncated).

## Layout

```
packages/browser-extension/
├── manifest.json               MV3 manifest
├── src/
│   ├── background.js           service worker, bridges panel ↔ native host
│   └── sidepanel/
│       ├── index.html
│       ├── sidepanel.css       dark-terminal theme
│       └── sidepanel.js        chat UI
├── native-host/
│   ├── obscura_native_host.py  stdio host, runs ObscuraClient.stream()
│   ├── com.obscura.host.json.tmpl
│   └── install.sh              installs the manifest into browser dirs
└── icons/
```
