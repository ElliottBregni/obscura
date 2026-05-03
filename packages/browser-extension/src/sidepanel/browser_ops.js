// browser-tool executor — runs chrome.scripting / chrome.tabs / chrome.debugger
// ops on behalf of the native host. Two families:
//
//   - Event dispatch (default): chrome.scripting.executeScript with a function
//     that synthesises DOM events. isTrusted=false; no banner.
//   - CDP: chrome.debugger.sendCommand. isTrusted=true; yellow banner appears
//     until cdp_detach is called. Requires `debugger` in manifest permissions.
//
// Entry point: handleBrowserTool(msg, postResponse) — `postResponse` is the
// callback used to send the browser-tool-response frame back to the host.

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function execInTab(tabId, func, args = []) {
  const res = await chrome.scripting.executeScript({
    target: { tabId, allFrames: false },
    func,
    args,
    world: "MAIN",
  });
  return res?.[0]?.result;
}

const cdpState = {
  attached: new Set(),
  consoleLogs: new Map(),
  networkLog: new Map(),
};

const CDP_LOG_LIMIT = 250;

function _cdpModifiers(modifiers) {
  let flags = 0;
  for (const m of modifiers || []) {
    if (m === "Alt") flags |= 1;
    else if (m === "Control" || m === "Ctrl") flags |= 2;
    else if (m === "Meta" || m === "Command") flags |= 4;
    else if (m === "Shift") flags |= 8;
  }
  return flags;
}

function _keyToCode(key) {
  if (key.length === 1) {
    const c = key.toUpperCase();
    if (c >= "A" && c <= "Z") return `Key${c}`;
    if (c >= "0" && c <= "9") return `Digit${c}`;
  }
  return key;
}

async function ensureCdpAttached(tabId) {
  if (cdpState.attached.has(tabId)) return;
  await chrome.debugger.attach({ tabId }, "1.3");
  cdpState.attached.add(tabId);
  cdpState.consoleLogs.set(tabId, []);
  cdpState.networkLog.set(tabId, []);
  await chrome.debugger.sendCommand({ tabId }, "Runtime.enable");
  await chrome.debugger.sendCommand({ tabId }, "Network.enable");
}

if (typeof chrome !== "undefined" && chrome.debugger) {
  chrome.debugger.onDetach.addListener((source) => {
    if (source.tabId !== undefined) {
      cdpState.attached.delete(source.tabId);
      cdpState.consoleLogs.delete(source.tabId);
      cdpState.networkLog.delete(source.tabId);
    }
  });

  chrome.debugger.onEvent.addListener((source, method, params) => {
    const tabId = source.tabId;
    if (tabId === undefined) return;
    if (method === "Runtime.consoleAPICalled") {
      const buf = cdpState.consoleLogs.get(tabId);
      if (!buf) return;
      const text = (params.args || [])
        .map((a) => a.value ?? a.description ?? a.unserializableValue ?? "")
        .map((s) => String(s).slice(0, 400))
        .join(" ");
      buf.push({ level: params.type || "log", text, ts: Date.now() });
      if (buf.length > CDP_LOG_LIMIT) buf.splice(0, buf.length - CDP_LOG_LIMIT);
    } else if (method === "Network.requestWillBeSent") {
      const buf = cdpState.networkLog.get(tabId);
      if (!buf) return;
      buf.push({
        requestId: params.requestId,
        method: params.request.method,
        url: params.request.url,
        ts: Date.now(),
      });
      if (buf.length > CDP_LOG_LIMIT) buf.splice(0, buf.length - CDP_LOG_LIMIT);
    } else if (method === "Network.responseReceived") {
      const buf = cdpState.networkLog.get(tabId);
      if (!buf) return;
      const rec = buf.find((e) => e.requestId === params.requestId);
      if (rec) {
        rec.status = params.response.status;
        rec.mime = params.response.mimeType;
      }
    }
  });
}

export async function handleBrowserTool(msg, postResponse) {
  const { id: reqId, op, args = {} } = msg;
  try {
    const result = await runBrowserOp(op, args);
    postResponse({ type: "browser-tool-response", id: reqId, ok: true, result });
  } catch (err) {
    postResponse({
      type: "browser-tool-response",
      id: reqId,
      ok: false,
      error: String(err?.message ?? err),
    });
  }
}

async function runBrowserOp(op, args) {
  const tab = await activeTab();
  if (!tab?.id && op !== "list_tabs") throw new Error("no active tab");

  switch (op) {
    case "read_page": {
      const { max_chars = 20000, include_html = false } = args;
      const payload = await execInTab(tab.id, (maxChars, includeHtml) => {
        const trim = (s, n) => (s.length > n ? s.slice(0, n) + "\n…[truncated]" : s);
        const text = trim(document.body?.innerText ?? "", maxChars);
        const headings = [...document.querySelectorAll("h1,h2,h3,h4")].slice(0, 80).map((h) => ({
          level: h.tagName.toLowerCase(),
          text: (h.innerText || "").trim().slice(0, 160),
        }));
        const links = [...document.querySelectorAll("a[href]")].slice(0, 60).map((a) => ({
          text: (a.innerText || "").trim().slice(0, 80),
          href: a.href,
        }));
        const forms = [...document.querySelectorAll("input,textarea,select")].slice(0, 40).map((el) => ({
          tag: el.tagName.toLowerCase(),
          type: el.type || "",
          name: el.name || "",
          id: el.id || "",
          placeholder: el.placeholder || "",
          value: el.type === "password" ? "" : (el.value || "").slice(0, 120),
        }));
        return {
          url: location.href,
          title: document.title,
          text,
          headings,
          links,
          fields: forms,
          html: includeHtml ? trim(document.body?.outerHTML ?? "", maxChars) : undefined,
        };
      }, [max_chars, include_html]);
      return payload;
    }
    case "query_selector": {
      const { selector, all = false } = args;
      return await execInTab(tab.id, (sel, fetchAll) => {
        const nodes = fetchAll
          ? [...document.querySelectorAll(sel)]
          : (document.querySelector(sel) ? [document.querySelector(sel)] : []);
        return nodes.slice(0, 50).map((el) => {
          const attrs = {};
          for (const a of el.attributes) attrs[a.name] = a.value;
          return {
            tag: el.tagName.toLowerCase(),
            text: (el.innerText || "").slice(0, 600),
            attrs,
          };
        });
      }, [selector, all]);
    }
    case "click": {
      const { selector } = args;
      return await execInTab(tab.id, (sel) => {
        const el = document.querySelector(sel);
        if (!el) return { ok: false, error: "no match" };
        el.click();
        return { ok: true };
      }, [selector]);
    }
    case "fill": {
      const { selector, value } = args;
      return await execInTab(tab.id, (sel, val) => {
        const el = document.querySelector(sel);
        if (!el) return { ok: false, error: "no match" };
        const proto = el.tagName === "TEXTAREA"
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
        if (setter) setter.call(el, val); else el.value = val;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return { ok: true };
      }, [selector, value]);
    }
    case "eval_js": {
      const { expression } = args;
      const wrapped = `(() => { try { return ${expression}; } catch(e) { return String(e); } })()`;
      const res = await execInTab(tab.id, (expr) => {
        // eslint-disable-next-line no-eval
        const v = eval(expr);
        try { return { ok: true, value: JSON.parse(JSON.stringify(v)) }; }
        catch { return { ok: true, value: String(v) }; }
      }, [wrapped]);
      return res;
    }
    case "list_tabs": {
      const tabs = await chrome.tabs.query({ currentWindow: true });
      return tabs.map((t) => ({
        id: t.id,
        title: t.title,
        url: t.url,
        active: t.active,
        pinned: t.pinned,
      }));
    }
    case "switch_tab": {
      await chrome.tabs.update(args.tab_id, { active: true });
      return { ok: true };
    }
    case "navigate": {
      await chrome.tabs.update(tab.id, { url: args.url });
      return { ok: true };
    }
    case "screenshot": {
      const dataUrl = await chrome.tabs.captureVisibleTab(null, { format: "png" });
      return { dataUrl };
    }
    case "wait_for_selector": {
      const { selector, timeout_ms = 10000 } = args;
      const res = await execInTab(tab.id, async (sel, timeoutMs) => {
        const deadline = Date.now() + timeoutMs;
        const poll = () => {
          const el = document.querySelector(sel);
          if (el) {
            return {
              ok: true,
              tag: el.tagName.toLowerCase(),
              text: (el.innerText || "").slice(0, 400),
            };
          }
          return null;
        };
        let hit = poll();
        if (hit) return hit;
        while (Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, 100));
          hit = poll();
          if (hit) return hit;
        }
        return { ok: false, error: "timeout" };
      }, [selector, timeout_ms]);
      return res;
    }
    case "get_selection": {
      return await execInTab(tab.id, () => {
        const sel = window.getSelection();
        const text = sel ? sel.toString() : "";
        let anchor = null;
        if (sel && sel.rangeCount > 0) {
          const range = sel.getRangeAt(0);
          const node = range.startContainer.nodeType === 1
            ? range.startContainer
            : range.startContainer.parentElement;
          if (node) {
            anchor = {
              tag: node.tagName?.toLowerCase() || "",
              id: node.id || "",
              class: node.className || "",
            };
          }
        }
        return { text, anchor, url: location.href };
      });
    }
    case "scroll_to": {
      const { selector, behavior = "smooth" } = args;
      return await execInTab(tab.id, (sel, bhv) => {
        const el = document.querySelector(sel);
        if (!el) return { ok: false, error: "no match" };
        el.scrollIntoView({ behavior: bhv, block: "center", inline: "nearest" });
        return { ok: true };
      }, [selector, behavior]);
    }
    case "new_tab": {
      const { url, active = true } = args;
      const created = await chrome.tabs.create({ url, active });
      return { id: created.id, url: created.url };
    }
    case "close_tab": {
      await chrome.tabs.remove(args.tab_id);
      return { ok: true };
    }
    case "reload_tab": {
      await chrome.tabs.reload(tab.id, { bypassCache: !!args.bypass_cache });
      return { ok: true };
    }
    case "go_back": {
      await chrome.tabs.goBack(tab.id);
      return { ok: true };
    }
    case "go_forward": {
      await chrome.tabs.goForward(tab.id);
      return { ok: true };
    }
    case "press_key": {
      const { key, modifiers = [], selector = null } = args;
      return await execInTab(tab.id, (k, mods, sel) => {
        let target;
        if (sel) {
          target = document.querySelector(sel);
          if (!target) return { ok: false, error: "no match" };
          target.focus?.();
        } else {
          target = document.activeElement || document.body;
        }
        const ctrl = mods.includes("Control") || mods.includes("Ctrl");
        const shift = mods.includes("Shift");
        const alt = mods.includes("Alt");
        const meta = mods.includes("Meta") || mods.includes("Command");
        const init = {
          key: k,
          code: k.length === 1 ? `Key${k.toUpperCase()}` : k,
          bubbles: true,
          cancelable: true,
          composed: true,
          ctrlKey: ctrl,
          shiftKey: shift,
          altKey: alt,
          metaKey: meta,
        };
        target.dispatchEvent(new KeyboardEvent("keydown", init));
        if (k.length === 1 && !ctrl && !meta) {
          target.dispatchEvent(new KeyboardEvent("keypress", init));
        }
        target.dispatchEvent(new KeyboardEvent("keyup", init));
        return {
          ok: true,
          target: {
            tag: target.tagName?.toLowerCase() || "",
            id: target.id || "",
          },
        };
      }, [key, modifiers, selector]);
    }
    case "clipboard_read": {
      try {
        const text = await navigator.clipboard.readText();
        return { text };
      } catch (e) {
        return { ok: false, error: String(e?.message ?? e) };
      }
    }
    case "clipboard_write": {
      const { text } = args;
      try {
        await navigator.clipboard.writeText(String(text ?? ""));
        return { ok: true };
      } catch (e) {
        return { ok: false, error: String(e?.message ?? e) };
      }
    }
    case "type_text": {
      const { text, selector = null } = args;
      if (selector) {
        await execInTab(tab.id, (sel) => {
          document.querySelector(sel)?.focus?.();
        }, [selector]);
      }
      await ensureCdpAttached(tab.id);
      await chrome.debugger.sendCommand(
        { tabId: tab.id },
        "Input.insertText",
        { text: String(text ?? "") },
      );
      return { ok: true };
    }
    case "native_press_key": {
      const { key, modifiers = [], selector = null } = args;
      if (selector) {
        await execInTab(tab.id, (sel) => {
          document.querySelector(sel)?.focus?.();
        }, [selector]);
      }
      await ensureCdpAttached(tab.id);
      const mods = _cdpModifiers(modifiers);
      const code = _keyToCode(key);
      await chrome.debugger.sendCommand({ tabId: tab.id }, "Input.dispatchKeyEvent", {
        type: "rawKeyDown", key, code, modifiers: mods,
      });
      if (key.length === 1 && (mods & 6) === 0) {
        await chrome.debugger.sendCommand({ tabId: tab.id }, "Input.dispatchKeyEvent", {
          type: "char", key, code, text: key, unmodifiedText: key, modifiers: mods,
        });
      }
      await chrome.debugger.sendCommand({ tabId: tab.id }, "Input.dispatchKeyEvent", {
        type: "keyUp", key, code, modifiers: mods,
      });
      return { ok: true };
    }
    case "native_click": {
      const { selector } = args;
      const rect = await execInTab(tab.id, (sel) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        el.scrollIntoView({ block: "center", inline: "center" });
        const r = el.getBoundingClientRect();
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      }, [selector]);
      if (!rect) return { ok: false, error: "no match" };
      await ensureCdpAttached(tab.id);
      await chrome.debugger.sendCommand({ tabId: tab.id }, "Input.dispatchMouseEvent", {
        type: "mouseMoved", x: rect.x, y: rect.y, button: "none",
      });
      await chrome.debugger.sendCommand({ tabId: tab.id }, "Input.dispatchMouseEvent", {
        type: "mousePressed", x: rect.x, y: rect.y, button: "left", clickCount: 1,
      });
      await chrome.debugger.sendCommand({ tabId: tab.id }, "Input.dispatchMouseEvent", {
        type: "mouseReleased", x: rect.x, y: rect.y, button: "left", clickCount: 1,
      });
      return { ok: true };
    }
    case "upload_file": {
      const { selector, paths } = args;
      if (!Array.isArray(paths) || paths.length === 0) {
        return { ok: false, error: "paths must be a non-empty array of absolute paths" };
      }
      await ensureCdpAttached(tab.id);
      const doc = await chrome.debugger.sendCommand({ tabId: tab.id }, "DOM.getDocument", {});
      const found = await chrome.debugger.sendCommand(
        { tabId: tab.id },
        "DOM.querySelector",
        { nodeId: doc.root.nodeId, selector },
      );
      if (!found?.nodeId) return { ok: false, error: "no match" };
      await chrome.debugger.sendCommand(
        { tabId: tab.id },
        "DOM.setFileInputFiles",
        { nodeId: found.nodeId, files: paths.map(String) },
      );
      return { ok: true, file_count: paths.length };
    }
    case "console_logs": {
      const { limit = 50 } = args;
      await ensureCdpAttached(tab.id);
      const buf = cdpState.consoleLogs.get(tab.id) || [];
      return { logs: buf.slice(-Number(limit)) };
    }
    case "network_log": {
      const { limit = 50, url_contains = null } = args;
      await ensureCdpAttached(tab.id);
      let buf = cdpState.networkLog.get(tab.id) || [];
      if (url_contains) {
        const needle = String(url_contains);
        buf = buf.filter((e) => e.url?.includes(needle));
      }
      return { entries: buf.slice(-Number(limit)) };
    }
    case "cdp_detach": {
      if (cdpState.attached.has(tab.id)) {
        try {
          await chrome.debugger.detach({ tabId: tab.id });
        } catch {
          // already detached — fine.
        }
      }
      cdpState.attached.delete(tab.id);
      cdpState.consoleLogs.delete(tab.id);
      cdpState.networkLog.delete(tab.id);
      return { ok: true };
    }
    default:
      throw new Error(`unknown op: ${op}`);
  }
}
