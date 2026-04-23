ExtBridge Bridge

Overview
This repo addition implements a robust page <-> extension bridge:
- Page-side injection script (Tampermonkey/inline) exposing window.ExtBridge
- Content script that validates and forwards requests to the extension
- Background handlers (service worker) that perform allowed actions and reply

Files added
- src/tools/extbridge.user.js - Tampermonkey page injector (robust, handshake)
- src/content_scripts/extbridge_content.js - content script that relays and enforces whitelist/limits

How to install (dev)
1. Copy src/tools/extbridge.user.js into Tampermonkey or paste the injector into the page console to test.
2. Load the extension unpacked: chrome://extensions -> Developer mode -> Load unpacked -> packages/browser-extension
   (manifest already points to src/background.js)
3. Ensure content_scripts entry exists in manifest (the repo now includes one). If you previously loaded the extension, reload after these changes.

Quick test
- Open a page matching https://*/* (e.g., https://example.com)
- Open console and run: window.ExtBridge.send('ping', {}).then(console.log).catch(console.error)

Handshakes and TTL
- The bridge supports an ephemeral ECDH handshake that derives an HMAC key for signing messages between the page and the extension.
- On the page: call await window.ExtBridge.initHandshake() to perform the ECDH exchange and derive a shared key.
- After initHandshake completes the background will return ttlMs (milliseconds) indicating how long the derived key is valid.
- Keys auto-expire after ttlMs (default 5 minutes). Call initHandshake again to renew the handshake.

Streaming demo
- Start a demo stream from the page: window.ExtBridge.send('stream', {chunks:['a','b','c'], intervalMs:400})
- Listen for chunks: window.ExtBridge.on(msg => { if(msg.stream) console.log('chunk', msg.chunk, 'done', msg.done) })

Security notes
- Do not expose secrets in page context. Keep tokens in chrome.storage and only provide minimal views to page scripts.
- Handshake keys expire automatically; long-running or sensitive operations should re-validate the session.
- Keep WHITELIST in content script and extension small. Validate payloads server-side.
- Use short timeouts and reject large payloads.

Next steps
- Add cancellation for streams and robust backpressure.
- Add automated tests for handshake, ping, batch and invalid payloads.
- Add a bootstrap installer for native-host and document usage.
