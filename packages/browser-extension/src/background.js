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
    broadcastToPanels(msg);
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
  });

  port.onMessage.addListener((msg) => {
    const host = ensureNative();
    if (!host) return;
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

// First-run onboarding: open the setup page when the extension is installed
// or updated across a breaking host-protocol change. Also verify the native
// host is reachable  if not, open onboarding automatically.
chrome.runtime.onInstalled.addListener(async (details) => {
  if (details.reason === "install") {
    const healthy = await healthCheck(5000);
    if (!healthy) {
      chrome.tabs.create({
        url: chrome.runtime.getURL("src/onboarding/index.html"),
      });
    }
  }
});

// Service worker startup health check  verify native host connectivity.
(async () => {
  try {
    const healthy = await healthCheck(5000);
    if (!healthy) {
      // Native host not responding  panels will show the disconnect banner.
      // No action needed here beyond logging for debugging.
      console.warn("[obscura] native host health check failed on startup");
    }
  } catch {
    // Swallow  best-effort check.
  }
})();

// -- Page bridge handler ----------------------------------------------------
// Handle messages forwarded from the content script (page bridge). Keep a
// small whitelist of allowed commands and implement batching for convenience.

const PAGE_BRIDGE_WHITELIST = new Set(['ping', 'getToken', 'batch']);

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
  return { error: 'unsupported' };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Messages from content script are expected to have 'from: page-bridge'
  if (!msg || msg.from !== 'page-bridge') return; // ignore other messages

  // basic validation
  if (typeof msg.cmd !== 'string' || !PAGE_BRIDGE_WHITELIST.has(msg.cmd)) {
    sendResponse({ error: 'cmd-not-allowed' });
    return;
  }

  // Process command
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
