// First-run onboarding page. Opens automatically via
// chrome.runtime.onInstalled from the service worker. Can also be opened
// manually via chrome-extension://<id>/src/onboarding/index.html.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

// Fill extension id from chrome.runtime.id.
const extId = chrome.runtime?.id ?? "(unknown — open this inside Chrome)";
$("#ext-id").textContent = extId;

// Probe the native host via the service worker.
function probeHost() {
  const pill = $("#host-status");
  const port = chrome.runtime.connect({ name: "sidepanel" });
  const timer = setTimeout(() => {
    pill.textContent = "not installed";
    pill.className = "pill error";
    try { port.disconnect(); } catch {}
  }, 2500);

  port.onMessage.addListener((msg) => {
    if (msg?.type === "ready" || msg?.type === "bridge-ready") {
      // bridge-ready = service worker bridge OK. We still need the host.
      if (msg.type === "ready") {
        clearTimeout(timer);
        pill.textContent = `connected · v${msg.version ?? "?"}`;
        pill.className = "pill ok";
        try { port.disconnect(); } catch {}
      }
    } else if (msg?.type === "error") {
      clearTimeout(timer);
      pill.textContent = "not installed";
      pill.className = "pill error";
      pill.title = msg.message ?? "";
      try { port.disconnect(); } catch {}
    }
  });

  port.onDisconnect.addListener(() => {
    // Error surfaces via the message handler; nothing to do here.
  });
}

probeHost();
// Re-probe every few seconds so the status line updates once the user
// finishes running install.sh.
setInterval(probeHost, 5000);

// ------ Tabs -------------------------------------------------------------

for (const tab of $$(".tab")) {
  tab.addEventListener("click", () => {
    const name = tab.dataset.tab;
    for (const t of $$(".tab")) t.classList.toggle("active", t === tab);
    for (const panel of $$(".cmd[data-tab]")) {
      panel.hidden = panel.dataset.tab !== name;
    }
  });
}

// ------ Copy buttons -----------------------------------------------------

for (const btn of $$(".copy")) {
  btn.addEventListener("click", async () => {
    const target = btn.dataset.copy;
    let text = "";
    if (target === "self") {
      text = btn.closest(".cmd")?.querySelector("code")?.textContent ?? "";
    } else {
      text = document.querySelector(target)?.textContent ?? "";
    }
    try {
      await navigator.clipboard.writeText(text.trim());
      const original = btn.textContent;
      btn.textContent = "copied";
      btn.classList.add("done");
      setTimeout(() => {
        btn.textContent = original;
        btn.classList.remove("done");
      }, 1200);
    } catch {
      btn.textContent = "blocked";
    }
  });
}

// ------ Actions ----------------------------------------------------------

$("#open-panel")?.addEventListener("click", async () => {
  // Chrome requires a gesture-initiated sidePanel.open call.
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.windowId != null && chrome.sidePanel?.open) {
      await chrome.sidePanel.open({ windowId: tab.windowId });
    }
  } catch (err) {
    alert(
      "Couldn't open the side panel from here. Click the Obscura toolbar " +
      "icon or use Chrome's side-panel launcher on the right of the address " +
      "bar.\n\n" + err.message,
    );
  }
});

$("#reload-host")?.addEventListener("click", () => {
  try {
    const port = chrome.runtime.connect({ name: "sidepanel" });
    port.postMessage({ type: "shutdown" });
    setTimeout(() => { try { port.disconnect(); } catch {} }, 500);
    setTimeout(probeHost, 800);
  } catch {}
});

$("#open-extensions")?.addEventListener("click", (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: "chrome://extensions" });
});
