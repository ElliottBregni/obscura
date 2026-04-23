ExtBridge Bridge

Overview
This repo addition implements a robust page <-> extension bridge:
- Page-side injection script (Tampermonkey/inline) exposing window.ExtBridge
- Content script that validates and forwards requests to the extension
- Background handlers (service worker) that perform allowed actions and reply

Files added
- scripts/extbridge.user.js - Tampermonkey page injector (robust, handshake)
- src/content_scripts/extbridge_content.js - content script that relays and enforces whitelist/limits

How to install (dev)
1. Copy scripts/extbridge.user.js into Tampermonkey or paste the injector into the page console to test.
2. Load the extension unpacked: chrome://extensions -> Developer mode -> Load unpacked -> packages/browser-extension
   (manifest already points to src/background.js)
3. Ensure content_scripts entry exists in manifest (the repo now includes one). If you previously loaded the extension, reload after these changes.

Quick test
- Open a page matching https://*/* (e.g., https://example.com)
- Open console and run: window.ExtBridge.send('ping', {}).then(console.log).catch(console.error)

Security notes
- Do not expose secrets in page context. Keep tokens in chrome.storage and only provide minimal views to page scripts.
- Keep WHITELIST in content script and extension small. Validate payloads server-side.
- Use short timeouts and reject large payloads.

Next steps
- Add batch/stream handlers to background.js for business commands.
- Add automated tests for handshake, ping, batch and invalid payloads.
