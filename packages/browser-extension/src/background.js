// Service worker: bridges the side-panel to the native messaging host.
//
// Protocol (extension <-> native host) is a stream of length-prefixed JSON
// objects. See native-host/obscura_native_host.py. Multiple concurrent
// requests are multiplexed by the `id` field.

const NATIVE_HOST_NAME = "com.obscura.host";

let nativePort = null;
/** @type {Set<chrome.runtime.Port>} */
const panelPorts = new Set();

// Per-Chrome-tab state tracking
const tabPanelState = new Map(); // chromeTabId -> { sessionId, lastLabel }

// Route host replies back to the panel that sent the request.
// msgId -> originating panel Port. Without this, every panel receives every
// reply and overwrites its own sessionId from another tab's conversation.
/** @type {Map<string, chrome.runtime.Port>} */
const requestOrigin = new Map();

// Handshake keys: handshakeId -> CryptoKey (HMAC key)
const handshakeKeys = new Map();

// Notify panels when the user switches Chrome tabs so each panel can swap
// its conversation to match the newly-active tab.
chrome.tabs.onActivated.addListener(({ tabId }) => {
  for (const port of panelPorts) {
    try {
      port.postMessage({ type: "tab_switched", tabId });
    } catch {
      // port gone; cleanup happens in onDisconnect
    }
  }
});

function broadcastToPanels(msg) {
  for (const p of panelPorts) {
    try {
      p.postMessage(msg);
    } catch {
      // panel gone; cleanup happens in onDisconnect
    }
  }
}

function ensureNative() {
  if (nativePort) return nativePort;
  try {
    nativePort = chrome.runtime.connectNative(NATIVE_HOST_NAME);
  } catch (err) {
    broadcastToPanels({
      type: "error",
      message: `Failed to launch native host (${NATIVE_HOST_NAME}): ${err.message}. ` +
        `Run packages/browser-extension/native-host/install.sh and reload the extension.`,
    });
    return null;
  }

  nativePort.onMessage.addListener((msg) => {
    const id = msg?.id;
    const origin = id != null ? requestOrigin.get(id) : null;
    if (origin) {
      try {
        origin.postMessage(msg);
      } catch {
        // origin panel gone; fall through to cleanup
      }
      // Terminal messages close the request — drop the route entry.
      const t = msg?.type;
      if (t === "done" || t === "error" || t === "cancelled") {
        requestOrigin.delete(id);
      }
    } else {
      // Unsolicited (ready, warnings without id, etc.) — broadcast.
      broadcastToPanels(msg);
    }
  });

  nativePort.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError?.message;
    if (err) {
      broadcastToPanels({
        type: "error",
        message: `Native host disconnected: ${err}. ` +
          `Check that obscura is installed in the Python env the host launches.`,
      });
    }
    nativePort = null;
  });

  return nativePort;
}

/**
 * Health-check: try to connect to the native host and see if it responds
 * within `timeoutMs`. Returns true if healthy, false otherwise.
 */
function healthCheck(timeoutMs = 5000) {
  return new Promise((resolve) => {
    let port;
    try {
      port = chrome.runtime.connectNative(NATIVE_HOST_NAME);
    } catch {
      resolve(false);
      return;
    }

    const timer = setTimeout(() => {
      try { port.disconnect(); } catch {}
      resolve(false);
    }, timeoutMs);

    port.onMessage.addListener((msg) => {
      clearTimeout(timer);
      // Any message means the host is alive. Disconnect the probe.
      try { port.disconnect(); } catch {}
      resolve(true);
    });

    port.onDisconnect.addListener(() => {
      clearTimeout(timer);
      resolve(false);
    });
  });
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "sidepanel") return;
  panelPorts.add(port);

  port.onDisconnect.addListener(() => {
    panelPorts.delete(port);
    for (const [id, p] of requestOrigin) {
      if (p === port) requestOrigin.delete(id);
    }
  });

  port.onMessage.addListener((msg) => {
    const host = ensureNative();
    if (!host) return;
    if (msg?.id != null) {
      requestOrigin.set(msg.id, port);
    }
    try {
      host.postMessage(msg);
    } catch (err) {
      port.postMessage({
        type: "error",
        id: msg?.id,
        message: `Send to native host failed: ${err.message}`,
      });
      nativePort = null;
    }
  });

  // Confirm bridge is alive to the panel immediately.
  port.postMessage({ type: "bridge-ready" });

  // Send the current active tab ID so the panel can load the right conversation.
  chrome.tabs.query({ active: true, currentWindow: true }).then(([activeTab]) => {
    if (activeTab?.id != null) {
      try { port.postMessage({ type: "tab_context", tabId: activeTab.id }); } catch {}
    }
  }).catch(() => {});
});

// Clicking the toolbar icon opens the side panel in the active tab.
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch(() => {});

// Service worker startup health check — verify native host connectivity.
(async () => {
  try {
    const healthy = await healthCheck(5000);
    if (!healthy) {
      // Native host not responding — panels will show the disconnect banner.
      // No action needed here beyond logging for debugging.
      console.warn("[obscura] native host health check failed on startup");
    }
  } catch {
    // Swallow — best-effort check.
  }
})();

// -- Page bridge handler ----------------------------------------------------
// Handle messages forwarded from the content script (page bridge). Keep a
// small whitelist of allowed commands and implement batching for convenience.

const PAGE_BRIDGE_WHITELIST = new Set(['ping', 'getToken', 'batch', 'stream', 'handshake-init']);

// helpers for base64 conversion
function arrayBufferToBase64(buf) {
  const bytes = new Uint8Array(buf);
  let binary = '';
  for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}
function base64ToArrayBuffer(b64) {
  const binary = atob(b64);
  const len = binary.length;
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

async function generateECDHKeyPair() {
  return crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
}

async function exportRawPublicKey(key) {
  const raw = await crypto.subtle.exportKey('raw', key);
  return arrayBufferToBase64(raw);
}

async function importRawPublicKey(b64) {
  const buf = base64ToArrayBuffer(b64);
  return crypto.subtle.importKey('raw', buf, { name: 'ECDH', namedCurve: 'P-256' }, true, []);
}

async function deriveHmacKey(ownPrivateKey, otherPublicKey) {
  // derive 256 bits and use as HMAC-SHA256 key
  const bits = await crypto.subtle.deriveBits({ name: 'ECDH', public: otherPublicKey }, ownPrivateKey, 256);
  return crypto.subtle.importKey('raw', bits, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']);
}

async function verifyHmacForMessage(handshakeId, message, signatureB64) {
  const key = handshakeKeys.get(handshakeId);
  if (!key) return false;
  const sigBuf = base64ToArrayBuffer(signatureB64);
  try {
    const ok = await crypto.subtle.verify('HMAC', key, sigBuf, new TextEncoder().encode(message));
    return ok;
  } catch (e) {
    return false;
  }
}

function canonicalizeMsgForHmac(msg) {
  return `${msg.id}|${msg.cmd}|${JSON.stringify(msg.payload || {})}`;
}

async function handleBridgeCommand(cmd, payload, sender) {
  if (cmd === 'ping') return { result: 'pong' };
  if (cmd === 'getToken') {
    // Example: retrieve auth token from storage (never expose raw secrets into page)
    const s = await chrome.storage.local.get(['authToken']);
    return { result: s.authToken || null };
  }
  if (cmd === 'batch') {
    // payload: [{cmd, payload}, ...]
    if (!Array.isArray(payload)) return { error: 'invalid-batch' };
    const results = [];
    for (const item of payload) {
      if (!item || typeof item.cmd !== 'string' || !PAGE_BRIDGE_WHITELIST.has(item.cmd)) {
        results.push({ error: 'cmd-not-allowed' });
        continue;
      }
      try {
        const r = await handleBridgeCommand(item.cmd, item.payload, sender);
        results.push(r);
      } catch (err) {
        results.push({ error: String(err) });
      }
    }
    return { result: results };
  }

  if (cmd === 'stream') {
    // Start a demo stream of incremental messages to the originating tab.
    // payload can include {intervalMs, chunks}
    const tabId = sender?.tab?.id;
    if (!tabId) return { error: 'no-originating-tab' };
    const intervalMs = (payload && payload.intervalMs) || 500;
    const chunks = (payload && payload.chunks) || ["one","two","three","done"];
    startDemoStream(tabId, payload && payload.id ? payload.id : crypto.randomUUID(), chunks, intervalMs);
    return { result: 'stream-started' };
  }

  return { error: 'unsupported' };
}

// Start a demo stream that sends incremental messages into the content script
// which will forward them into the page. This is a simple example; real
// streams should support cancellation and backpressure.
function startDemoStream(tabId, id, chunks, intervalMs) {
  let i = 0;
  const timer = setInterval(() => {
    const chunk = chunks[i] ?? null;
    const done = i >= chunks.length;
    try {
      chrome.tabs.sendMessage(tabId, { from: 'background-stream', id, chunk, done });
    } catch (e) {
      // ignore send errors
    }
    if (done) {
      clearInterval(timer);
    }
    i++;
  }, intervalMs);
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Messages from content script are expected to have 'from: page-bridge'
  if (!msg || msg.from !== 'page-bridge') return; // ignore other messages

  // basic validation
  if (typeof msg.cmd !== 'string' || !PAGE_BRIDGE_WHITELIST.has(msg.cmd)) {
    sendResponse({ error: 'cmd-not-allowed' });
    return;
  }

  // Special-case: handshake-init -> perform ECDH key exchange and return
  // the background public key. Expect payload: { pubkey: <base64> }
  if (msg.cmd === 'handshake-init') {
    (async () => {
      try {
        if (!msg.payload || typeof msg.payload.pubkey !== 'string') {
          sendResponse({ error: 'missing-pubkey' });
          return;
        }
        const clientPubB64 = msg.payload.pubkey;
        // generate our ECDH keypair
        const pair = await generateECDHKeyPair();
        // import client's public key
        const clientPub = await importRawPublicKey(clientPubB64);
        // derive HMAC key and store it by handshake id (msg.handshake expected)
        const hmacKey = await deriveHmacKey(pair.privateKey, clientPub);
        if (msg.handshake) {
          handshakeKeys.set(msg.handshake, hmacKey);
        }
        const ourPubB64 = await exportRawPublicKey(pair.publicKey);
        sendResponse({ result: { backgroundPubKey: ourPubB64 } });
      } catch (err) {
        sendResponse({ error: String(err) });
      }
    })();
    return true; // async
  }

  // For non-handshake messages (except ping), require a valid signature
  if (msg.cmd !== 'ping') {
    if (!msg.handshake || !msg.signature) {
      sendResponse({ error: 'missing-signature-or-handshake' });
      return;
    }
    const canon = canonicalizeMsgForHmac(msg);
    (async () => {
      const ok = await verifyHmacForMessage(msg.handshake, canon, msg.signature).catch(() => false);
      if (!ok) { sendResponse({ error: 'invalid-signature' }); return; }
      try {
        const out = await handleBridgeCommand(msg.cmd, msg.payload, sender);
        sendResponse(out);
      } catch (err) { sendResponse({ error: String(err) }); }
    })();
    return true; // async
  }

  // Process command (ping)
  (async () => {
    try {
      const out = await handleBridgeCommand(msg.cmd, msg.payload, sender);
      sendResponse(out);
    } catch (err) {
      sendResponse({ error: String(err) });
    }
  })();

  return true; // async
});
