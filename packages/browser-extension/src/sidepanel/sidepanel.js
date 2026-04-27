// Side-panel UI. Talks to the service worker on a long-lived Port, which
// proxies to the native messaging host running obscura.

const $ = (sel) => document.querySelector(sel);

const MsgType = {
  BRIDGE_READY: "bridge-ready",
  READY: "ready",
  CHUNK: "chunk",
  THINKING: "thinking",
  TOOL_START: "tool_start",
  TOOL_DELTA: "tool_delta",
  TOOL_END: "tool_end",
  TOOL_RESULT: "tool_result",
  DONE: "done",
  WIDGET: "widget",
  WIDGET_RESPONSE: "widget_response",
  ERROR: "error",
  WARNING: "warning",
  KAIROS: "kairos",
  DIAG: "diag",
  FLEET: "fleet",
  SEND: "send",
  COMMAND: "command",
  CANCEL: "cancel",
  PING: "ping",
  PONG: "pong",
  LIST_SESSIONS: "list_sessions",
  SESSIONS: "sessions",
};

const StorageManager = {
  async get(keys) {
    try { return await chrome.storage.local.get(keys); } catch { return {}; }
  },
  async set(obj) {
    try { await chrome.storage.local.set(obj); } catch (e) { console.warn("storage write failed", e); }
  },
  async load(key, defaults = {}) {
    const store = await this.get([key]);
    return { ...defaults, ...(store[key] ?? {}) };
  },
  async save(key, value) {
    await this.set({ [key]: value });
  },
};

const log = $("#log");
const form = $("#composer");
const input = $("#prompt");
const sendBtn = $("#send");
const status = $("#status");
const backendSel = $("#backend");
const workspaceSel = $("#workspace");
const sessionPicker = $("#session-picker");
const ctxToggle = $("#include-context");
const clearBtn = $("#clear");
const reloadHostBtn = $("#reload-host");
const stopBtn = $("#stop");
const sbBackend = $("#sb-backend");
const sbHost = $("#sb-host");
const sbGit = $("#sb-git");
const sbKairos = $("#sb-kairos");
const sbRewind = $("#sb-rewind");
const sbFleet = $("#sb-fleet");
const sbMcp = $("#sb-mcp");
const sbDiag = $("#sb-diag");
const sbExport = $("#sb-export");
const sbTheme = $("#sb-theme");
const tabStrip = $("#tab-strip");
const shortcutsOverlay = $("#shortcuts-overlay");
const shortcutsClose = $("#shortcuts-close");
const dropPreview = $("#drop-preview");
const diagOverlay = $("#diag-overlay");
const diagContent = $("#diag-content");
const diagClose = $("#diag-close");
const authGate = $("#auth-gate");
const authTokenInput = $("#auth-token-input");
const authSubmit = $("#auth-submit");
const warningBanner = $("#warning-banner");
const warningText = $("#warning-text");
const checkpointModal = $("#checkpoint-modal");
const checkpointClose = $("#checkpoint-close");
const checkpointNameInput = $("#checkpoint-name-input");
const checkpointSaveBtn = $("#checkpoint-save-btn");
const checkpointList = $("#checkpoint-list");
const fleetOverlay = $("#fleet-overlay");
const fleetClose = $("#fleet-close");
const fleetContent = $("#fleet-content");
const mcpOverlay = $("#mcp-overlay");
const mcpClose = $("#mcp-close");
const mcpDiscoverBtn = $("#mcp-discover-btn");
const mcpContent = $("#mcp-content");
const micBtn = $("#mic-btn");
const recentSessionsBtn = $("#recent-sessions-btn");
const recentSessionsModal = $("#recent-sessions-modal");
const recentSessionsClose = $("#recent-sessions-close");
const recentSessionsList = $("#recent-sessions-list");
let recentSessionsRequestId = null;
let recentSessionsTimeout = null;

// Storage keys. Bump STORAGE_VERSION any time a stored schema changes and
// add a migration in `migrateStorage()`. chrome.storage.local is already
// per-Chrome-profile, so these keys don't need profile scoping.
const STORAGE_VERSION = 1;
const STORAGE_VERSION_KEY = "obscura.storage.version";
const SETTINGS_KEY = "obscura.settings.v1";
const TRANSCRIPT_KEY = "obscura.transcript.v1";
const SESSIONS_KEY = "obscura.sessions.v1";
const TABS_KEY = "obscura.tabs.v1";
const THEME_KEY = "obscura_theme";
const TOOL_PERMS_KEY = "obscura_tool_perms";
const PROFILE_ID_KEY = "obscura.profile_id";

let port = null;
let sessionId = null;            // per-panel conversation id (from host)
let pending = new Map();         // msgId -> { bubble, toolBox, toolMap, streamedText, thinkingText, thinkingEl }
let busy = false;
let authToken = null;            // shared-secret token for the native host (OBSCURA_AUTH_TOKEN)
let commandIndex = [];           // from ready.commands ({name, doc, subcommands})
let skillIndex = [];             // from ready.skills       (string[])
let atCommandIndex = [];         // from ready.at_commands  (string[])
let userHistory = [];            // prompts the user has sent, for ↑/↓ nav
let historyCursor = -1;          // -1 = at the live input
let historyDraft = "";           // saved current input when stepping into history
let kairosState = "off";        // "on" | "off"
let pendingImages = [];          // { dataUrl, name }[] for drag-drop / paste
let pendingTextFiles = [];       // { name, content }[] for drag-drop
let checkpointPendingId = null;  // msgId of the in-flight /checkpoint command
let fleetPendingId = null;       // msgId of the in-flight /agent list command
let mcpPendingId = null;         // msgId of the in-flight /mcp list|discover command

// Per-Chrome-tab session persistence: track which Chrome tab this panel belongs to.
let chromeTabId = null;
chrome.tabs.getCurrent().then(tab => { chromeTabId = tab?.id ?? null; }).catch(() => {});

// ---------------------------------------------------------------------------
// Multi-session tabs

class TabManager {
  constructor(maxTabs = 8) {
    this._tabs = [];
    this._activeIdx = 0;
    this._maxTabs = maxTabs;
  }

  get count() { return this._tabs.length; }
  get activeIdx() { return this._activeIdx; }
  get active() { return this._tabs[this._activeIdx] ?? null; }
  get all() { return this._tabs; }

  create(label = "new") {
    if (this._tabs.length >= this._maxTabs) return false;
    const tab = { id: crypto.randomUUID(), label, sessionId: null, logHTML: "", pending: new Map(), streamStates: {} };
    this._tabs.push(tab);
    return tab;
  }

  close(idx) {
    if (this._tabs.length <= 1) return false;
    this._tabs.splice(idx, 1);
    if (this._activeIdx >= this._tabs.length) this._activeIdx = this._tabs.length - 1;
    return true;
  }

  saveActive(logHTML, sessionId, pending, streamStates) {
    const t = this.active;
    if (!t) return;
    t.logHTML = logHTML;
    t.sessionId = sessionId;
    t.pending = pending;
    t.streamStates = streamStates ?? {};
  }

  activate(idx) {
    if (idx < 0 || idx >= this._tabs.length || idx === this._activeIdx) return false;
    this._activeIdx = idx;
    return true;
  }

  getById(id) {
    return this._tabs.find(t => t.id === id) ?? null;
  }
}

const tabManager = new TabManager();

function createTab(label = "new", activate = true) {
  const tab = tabManager.create((label || "new").slice(0, 20));
  if (!tab) return;
  if (activate) switchTab(tabManager.count - 1);
  renderTabs();
  saveTabs();
}

function closeTab(idx) {
  tabManager.saveActive(log.innerHTML, sessionId, pending);
  if (!tabManager.close(idx)) return;
  restoreTab(tabManager.activeIdx);
  renderTabs();
  saveTabs();
}

function switchTab(idx) {
  if (idx === tabManager.activeIdx && tabManager.count > 0) return;
  // save current tab state
  tabManager.saveActive(log.innerHTML, sessionId, pending);
  tabManager.activate(idx);
  restoreTab(idx);
  renderTabs();
  saveTabs();
}

function restoreTab(idx) {
  const tab = tabManager.all[idx];
  if (!tab) return;
  log.innerHTML = tab.logHTML;
  sessionId = tab.sessionId;
  pending = tab.pending || new Map();
  scrollToBottom();
}

// --- Persistence: serialise the tab strip across panel reloads. -----------
// We snapshot the active tab into the in-memory record before writing so the
// stored copy reflects the current transcript/sessionId.

function saveTabs() {
  try {
    tabManager.saveActive(log.innerHTML, sessionId, pending);
    const tabs = tabManager.all.map((t) => ({
      id: t.id,
      label: t.label,
      sessionId: t.sessionId,
      logHTML: t.logHTML,
    }));
    chrome.storage.local.set({ [TABS_KEY]: { tabs, activeIdx: tabManager.activeIdx } });
  } catch (err) {
    console.warn("[obscura] saveTabs failed", err);
  }
}

async function loadTabs() {
  try {
    const store = await chrome.storage.local.get([TABS_KEY]);
    const state = store[TABS_KEY];
    if (!state || !Array.isArray(state.tabs) || state.tabs.length === 0) return false;
    tabManager._tabs = state.tabs.map((t) => ({
      id: t.id || crypto.randomUUID(),
      label: typeof t.label === "string" && t.label ? t.label : "session",
      sessionId: t.sessionId ?? null,
      logHTML: typeof t.logHTML === "string" ? t.logHTML : "",
      pending: new Map(),
      streamStates: {},
    }));
    const idx = Math.min(Math.max(state.activeIdx | 0, 0), tabManager._tabs.length - 1);
    tabManager._activeIdx = idx;
    restoreTab(idx);
    renderTabs();
    return true;
  } catch (err) {
    console.warn("[obscura] loadTabs failed", err);
    return false;
  }
}

// --- Inline rename: dbl-click swaps label <span> for an <input>. ---------
// Enter / blur commit, Escape reverts. Empty value reverts to original.
let _editingTabIdx = -1;

function beginRenameTab(idx, labelEl) {
  if (_editingTabIdx === idx) return;
  _editingTabIdx = idx;
  const tab = tabManager.all[idx];
  if (!tab) return;
  const original = tab.label;
  const inp = document.createElement("input");
  inp.type = "text";
  inp.className = "tab-label tab-label-edit";
  inp.value = original;
  inp.maxLength = 40;
  let settled = false;
  const commit = (next) => {
    if (settled) return;
    settled = true;
    _editingTabIdx = -1;
    const trimmed = (next || "").trim().slice(0, 20);
    tab.label = trimmed || original || "untitled";
    renderTabs();
    saveTabs();
  };
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(inp.value); }
    else if (e.key === "Escape") { e.preventDefault(); settled = true; _editingTabIdx = -1; renderTabs(); }
    e.stopPropagation();
  });
  inp.addEventListener("blur", () => commit(inp.value));
  inp.addEventListener("click", (e) => e.stopPropagation());
  inp.addEventListener("dblclick", (e) => e.stopPropagation());
  labelEl.replaceWith(inp);
  inp.focus();
  inp.select();
}

// --- Drag-to-reorder. ----------------------------------------------------
let _dragSrcIdx = -1;

function _clearDropMarkers() {
  for (const el of tabStrip.querySelectorAll(".tab-item.drop-before, .tab-item.drop-after")) {
    el.classList.remove("drop-before", "drop-after");
  }
}

function renderTabs() {
  tabStrip.innerHTML = "";
  if (tabManager.count <= 1) return; // hide tab strip when only 1 tab
  tabManager.all.forEach((tab, i) => {
    const el = document.createElement("div");
    el.className = "tab-item" + (i === tabManager.activeIdx ? " active" : "");
    el.draggable = true;
    const label = document.createElement("span");
    label.className = "tab-label";
    label.textContent = tab.label;
    label.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      beginRenameTab(i, label);
    });
    const close = document.createElement("span");
    close.className = "tab-close";
    close.textContent = "×";
    close.addEventListener("click", (e) => { e.stopPropagation(); closeTab(i); });
    el.append(label, close);
    el.addEventListener("click", () => switchTab(i));

    // Drag-and-drop reorder.
    el.addEventListener("dragstart", (e) => {
      _dragSrcIdx = i;
      try { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(i)); } catch {}
      el.classList.add("dragging");
    });
    el.addEventListener("dragend", () => {
      _dragSrcIdx = -1;
      el.classList.remove("dragging");
      _clearDropMarkers();
    });
    el.addEventListener("dragover", (e) => {
      if (_dragSrcIdx < 0 || _dragSrcIdx === i) return;
      e.preventDefault();
      try { e.dataTransfer.dropEffect = "move"; } catch {}
      _clearDropMarkers();
      const rect = el.getBoundingClientRect();
      const before = (e.clientX - rect.left) < rect.width / 2;
      el.classList.add(before ? "drop-before" : "drop-after");
    });
    el.addEventListener("dragleave", () => {
      el.classList.remove("drop-before", "drop-after");
    });
    el.addEventListener("drop", (e) => {
      e.preventDefault();
      const src = _dragSrcIdx;
      _clearDropMarkers();
      if (src < 0 || src === i) return;
      const rect = el.getBoundingClientRect();
      const before = (e.clientX - rect.left) < rect.width / 2;
      let dst = before ? i : i + 1;
      // Snapshot active tab before reordering so we don't lose live transcript.
      tabManager.saveActive(log.innerHTML, sessionId, pending);
      const activeId = tabManager.active?.id ?? null;
      const [moved] = tabManager._tabs.splice(src, 1);
      if (src < dst) dst -= 1;
      tabManager._tabs.splice(dst, 0, moved);
      // Re-anchor activeIdx to whatever was active before.
      if (activeId) {
        const newIdx = tabManager._tabs.findIndex((t) => t.id === activeId);
        if (newIdx >= 0) tabManager._activeIdx = newIdx;
      }
      renderTabs();
      saveTabs();
    });

    tabStrip.appendChild(el);
  });
  const plus = document.createElement("span");
  plus.className = "tab-new";
  plus.textContent = "+";
  plus.addEventListener("click", () => {
    createTab("new", true);
  });
  tabStrip.appendChild(plus);
}

// Initialize first tab
tabManager.create("session");

// ---------------------------------------------------------------------------
// Settings + transcript persistence

async function loadSettings() {
  try {
    const store = await StorageManager.get([SETTINGS_KEY, TRANSCRIPT_KEY]);
    const s = store[SETTINGS_KEY];
    if (s?.backend) backendSel.value = s.backend;
    if (typeof s?.includeContext === "boolean") ctxToggle.checked = s.includeContext;
    if (Array.isArray(s?.history)) userHistory = s.history.slice(-100);

    const t = store[TRANSCRIPT_KEY];
    if (t?.entries && Array.isArray(t.entries)) {
      sessionId = t.sessionId ?? null;
      for (const e of t.entries) {
        if (e.role && typeof e.text === "string") {
          const body = addMessage(e.role, "");
          if (e.role === "assistant") renderMarkdown(body, e.text);
          else body.textContent = e.text;
        }
      }
      if (t.entries.length > 0) {
        const restored = document.createElement("div");
        restored.className = "restore-line";
        restored.textContent = "— restored from last session —";
        log.appendChild(restored);
      }
    }
  } catch {}
}

function saveSettings() {
  StorageManager.save(SETTINGS_KEY, {
    backend: backendSel.value,
    includeContext: ctxToggle.checked,
    history: userHistory.slice(-100),
  });
}

let transcriptTimer = null;
function scheduleTranscriptSave() {
  if (transcriptTimer) clearTimeout(transcriptTimer);
  transcriptTimer = setTimeout(() => {
    transcriptTimer = null;
    const entries = [];
    for (const msg of log.querySelectorAll(".msg")) {
      const role = [...msg.classList].find((c) =>
        ["user", "assistant", "system", "error"].includes(c),
      );
      if (!role || role === "system" || role === "error") continue;
      // Use the stream-accumulated raw text when available (markdown source).
      const text = msg.dataset.raw ?? msg.querySelector(".body")?.textContent ?? "";
      if (text) entries.push({ role, text });
    }
    // Keep only the most recent 40 turns to cap storage.
    const trimmed = entries.slice(-80);
    chrome.storage.local.set({
      [TRANSCRIPT_KEY]: { sessionId, entries: trimmed, savedAt: Date.now() },
    });
  }, 400);
}

backendSel.addEventListener("change", saveSettings);
ctxToggle.addEventListener("change", saveSettings);

// ---------------------------------------------------------------------------
// Per-Chrome-tab state persistence

async function loadTabState() {
  if (!chromeTabId) return;
  const key = `tab_state_${chromeTabId}`;
  try {
    const store = await chrome.storage.local.get([key]);
    const state = store[key];
    if (state?.logHTML) {
      log.innerHTML = state.logHTML;
      sessionId = state.sessionId ?? null;
      scrollToBottom();
    }
  } catch {}
}

async function saveTabState() {
  if (!chromeTabId) return;
  const key = `tab_state_${chromeTabId}`;
  try {
    await chrome.storage.local.set({
      [key]: { logHTML: log.innerHTML, sessionId, timestamp: Date.now() },
    });
  } catch {}
}

window.addEventListener("beforeunload", saveTabState);

// ---------------------------------------------------------------------------
// Session metadata persistence

async function saveSessionMeta(sid, firstPrompt) {
  if (!sid) return;
  try {
    const store = await StorageManager.get([SESSIONS_KEY]);
    let sessions = store[SESSIONS_KEY] || [];
    // Remove duplicate
    sessions = sessions.filter((s) => s.id !== sid);
    sessions.unshift({
      id: sid,
      title: (firstPrompt || "").slice(0, 60),
      timestamp: Date.now(),
    });
    // Keep max 10
    sessions = sessions.slice(0, 10);
    await StorageManager.set({ [SESSIONS_KEY]: sessions });
    renderSessionPicker(sessions);
  } catch {}
}

async function loadSessionPicker() {
  try {
    const store = await StorageManager.get([SESSIONS_KEY]);
    const sessions = store[SESSIONS_KEY] || [];
    renderSessionPicker(sessions);
  } catch {}
}

function renderSessionPicker(sessions) {
  sessionPicker.innerHTML = '<option value="">current</option>';
  for (const s of sessions) {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.title || s.id.slice(0, 12);
    if (s.id === sessionId) opt.selected = true;
    sessionPicker.appendChild(opt);
  }
}

sessionPicker.addEventListener("change", () => {
  const sid = sessionPicker.value;
  if (!sid) return;
  // Tell host to resume this session
  try {
    port?.postMessage({ type: MsgType.SEND, id: crypto.randomUUID?.() ?? `r-${Date.now()}`, prompt: `/resume ${sid}`, backend: backendSel.value });
  } catch {}
});

// ---------------------------------------------------------------------------
// Message rendering

function addMessage(role, text) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const r = document.createElement("div");
  r.className = "role";
  r.textContent = role;
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text;
  wrap.append(r, body);
  log.appendChild(wrap);
  scrollToBottom();
  return body;
}

function scrollToBottom() {
  log.scrollTop = log.scrollHeight;
}

// Queue of prompts the user typed while a turn was in flight.  Each entry is
// a SendDescriptor: { id, prompt, isCommand, context, userBubble }.  We
// render the user bubble immediately (dimmed via `.queued`) so the panel
// feels responsive, and drain one item at a time as turns complete.
let sendQueue = [];
let _liveStatus = "";

function setBusy(v) {
  busy = v;
  // Keep the send button enabled — users can queue follow-ups while a turn
  // is streaming; see sendQueue / dispatchSend below.
  sendBtn.disabled = false;
  stopBtn.hidden = !v;
  renderStatus();
}

function setLiveStatus(text) {
  _liveStatus = text || "";
  renderStatus();
}

function renderStatus() {
  const base = busy ? _liveStatus || "running…" : _liveStatus;
  const q = sendQueue.length;
  status.textContent = q > 0 ? (base ? `${base} · ${q} queued` : `${q} queued`) : base;
  status.classList.toggle("busy", busy);
}

// ---------------------------------------------------------------------------
// Minimal markdown → DOM renderer.
// Zero-dep; handles fenced code, inline code, bold, italic, links, lists.

import { escHtml, markdownToHtml } from "./markdown.js";
import { withProfileId } from "./messaging.js";

function renderMarkdown(target, raw) {
  // Freeze the source so the transcript saver can grab original markdown.
  target.parentElement.dataset.raw = raw;

  // Delegate the pure transform to ./markdown.js (covered by vitest).
  target.innerHTML = markdownToHtml(raw);

  // Attach live copy buttons to each code block — DOM side effects stay
  // here because markdown.js is deliberately DOM-free.
  for (const pre of target.querySelectorAll("pre.code")) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "code-copy";
    btn.textContent = "copy";
    btn.addEventListener("click", async () => {
      await navigator.clipboard.writeText(pre.querySelector("code").textContent);
      btn.textContent = "copied";
      setTimeout(() => (btn.textContent = "copy"), 1200);
    });
    pre.appendChild(btn);
  }
}

// ---------------------------------------------------------------------------
// Reasoning / thinking blocks

function ensureThinkingBlock(msgId) {
  const st = pending.get(msgId);
  if (!st) return null;
  if (st.thinkingEl) return st.thinkingEl;

  const container = st.bubble.parentElement;
  const details = document.createElement("details");
  details.className = "thinking-block";
  const summary = document.createElement("summary");
  summary.textContent = "reasoning";
  const textDiv = document.createElement("div");
  textDiv.className = "thinking-text";
  details.append(summary, textDiv);

  // Insert before the body div
  container.insertBefore(details, st.bubble);
  st.thinkingEl = textDiv;
  st.thinkingText = "";
  return textDiv;
}

// ---------------------------------------------------------------------------
// Tool-call inline rendering

function ensureToolBox(msgId) {
  let st = pending.get(msgId);
  if (!st) return null;
  if (st.toolBox) return st.toolBox;
  const bubble = st.bubble;
  const container = bubble.parentElement;
  const toolBox = document.createElement("div");
  toolBox.className = "toolbox";
  // Insert tools before the streaming body.
  container.insertBefore(toolBox, bubble);
  st.toolBox = toolBox;
  st.toolMap = new Map();
  return toolBox;
}

function toolStart(msgId, toolUseId, toolName) {
  const box = ensureToolBox(msgId);
  if (!box) return;
  const st = pending.get(msgId);
  const card = document.createElement("details");
  card.className = "tool-card";
  card.open = false;
  const summary = document.createElement("summary");
  summary.innerHTML =
    `<span class="tool-chev">▸</span>` +
    `<span class="tool-name">${escHtml(toolName || "tool")}</span>` +
    `<span class="tool-dot"></span>`;
  const inputEl = document.createElement("pre");
  inputEl.className = "tool-input";
  const resultEl = document.createElement("pre");
  resultEl.className = "tool-result";
  resultEl.hidden = true;
  card.append(summary, inputEl, resultEl);
  box.appendChild(card);
  st.toolMap.set(toolUseId, { card, inputEl, resultEl, inputText: "" });
  setLiveStatus(`tool · ${toolName}`);
  scrollToBottom();
}

function toolDelta(msgId, toolUseId, delta) {
  const st = pending.get(msgId);
  const tool = st?.toolMap?.get(toolUseId);
  if (!tool) return;
  tool.inputText += delta;
  tool.inputEl.textContent = tool.inputText;
}

function toolEnd(msgId, toolUseId) {
  const st = pending.get(msgId);
  const tool = st?.toolMap?.get(toolUseId);
  if (!tool) return;
  // Try to pretty-print JSON input
  try {
    const parsed = JSON.parse(tool.inputText);
    tool.inputEl.textContent = JSON.stringify(parsed, null, 2);
  } catch {}
  tool.card.classList.add("done");
}

function toolResult(msgId, toolUseId, text) {
  const st = pending.get(msgId);
  const tool = st?.toolMap?.get(toolUseId);
  if (!tool) return;
  tool.resultEl.hidden = false;
  tool.resultEl.textContent = text.length > 4000 ? text.slice(0, 4000) + "…" : text;
  tool.card.classList.add("has-result");
}

// ---------------------------------------------------------------------------
// Live status (inside sidepanel, not just the footer).
// The canonical implementation lives next to setBusy — this block is kept
// as a documentation anchor for the former signature.

// ---------------------------------------------------------------------------
// Rich widget detail rendering
//
// Each helper returns either a DocumentFragment / element to append, or null
// to signal "I don't handle this shape — fall through to the generic table."
// Keep helpers small (~30 LOC). Tool names are matched case-insensitively
// because backends spell them differently (`Bash` vs `bash` vs `run_shell`).

// CDP-attaching browser tools: trigger Chrome's yellow "started debugging"
// banner, so we surface that cost in the widget. Keep this mirrored with the
// CDP family in packages/browser-extension/ARCHITECTURE.md.
const CDP_BROWSER_TOOLS = new Set([
  "browser_type_text",
  "browser_native_click",
  "browser_native_press_key",
  "browser_upload_file",
  "browser_console_logs",
  "browser_network_log",
  "browser_cdp_detach",
]);

const PATH_LIKE_KEYS = new Set([
  "path", "file_path", "filename", "filepath", "dir", "directory", "cwd", "root",
]);

function _truncate(str, n) {
  if (typeof str !== "string") return String(str);
  return str.length > n ? str.slice(0, n) + "\n…[truncated]" : str;
}

function _label(text, color) {
  const el = document.createElement("div");
  el.className = "w-detail-label";
  if (color) el.style.color = color;
  el.textContent = text;
  return el;
}

function _codeBlock(text, { borderColor } = {}) {
  const pre = document.createElement("pre");
  pre.className = "code";
  if (borderColor) pre.style.borderLeftColor = borderColor;
  const code = document.createElement("code");
  code.textContent = text;
  pre.appendChild(code);
  return pre;
}

function _path(text) {
  const span = document.createElement("div");
  span.className = "w-path";
  span.textContent = text;
  return span;
}

function _chipRow(items) {
  // items = [{label, value, kind?}]; skips empty values.
  const row = document.createElement("div");
  row.className = "w-chip-row";
  let any = false;
  for (const { label, value, kind } of items) {
    if (value === undefined || value === null || value === "") continue;
    const chip = document.createElement("span");
    chip.className = "w-chip" + (kind ? ` w-chip-${kind}` : "");
    chip.textContent = label ? `${label}: ${value}` : String(value);
    row.appendChild(chip);
    any = true;
  }
  return any ? row : null;
}

function _cdpBanner() {
  const chip = document.createElement("span");
  chip.className = "w-chip w-chip-cdp";
  chip.title = "This tool attaches chrome.debugger and shows a yellow 'started debugging' banner.";
  chip.textContent = "CDP — yellow banner";
  return chip;
}

function _expandableValue(text, threshold = 400) {
  // Returns a span; if `text` exceeds threshold, adds a "show more" toggle.
  const wrap = document.createElement("span");
  wrap.className = "w-value";
  if (text.length <= threshold) {
    wrap.textContent = text;
    return wrap;
  }
  const short = document.createElement("span");
  short.textContent = text.slice(0, threshold) + "…";
  const full = document.createElement("span");
  full.textContent = text;
  full.hidden = true;
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "w-show-more";
  toggle.textContent = "show more";
  toggle.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const expanded = !full.hidden;
    full.hidden = expanded;
    short.hidden = !expanded;
    toggle.textContent = expanded ? "show more" : "show less";
  });
  wrap.append(short, full, toggle);
  return wrap;
}

// --- per-tool helpers ------------------------------------------------------

function _renderShellLike(detail) {
  const cmd = detail.command || detail.expression || detail.code || detail.cmd || "";
  if (!cmd) return null;
  const frag = document.createDocumentFragment();
  if (detail.cwd || detail.timeout || detail.description) {
    const chips = _chipRow([
      { label: "cwd", value: detail.cwd },
      { label: "timeout", value: detail.timeout },
      { label: "desc", value: detail.description },
    ]);
    if (chips) frag.appendChild(chips);
  }
  frag.appendChild(_codeBlock(_truncate(cmd, 4000)));
  return frag;
}

function _renderFileWrite(detail, toolName) {
  const content = detail.content || detail.new_string || detail.text || "";
  if (!content) return null;
  const frag = document.createDocumentFragment();
  frag.appendChild(_path(detail.file_path || detail.path || toolName));
  frag.appendChild(_codeBlock(_truncate(content, 4000)));
  if (detail.old_string) {
    frag.appendChild(_label("replaces:", "var(--red)"));
    frag.appendChild(_codeBlock(_truncate(detail.old_string, 2000), { borderColor: "var(--red)" }));
  }
  return frag;
}

function _renderReadFile(detail) {
  const p = detail.file_path || detail.path;
  if (!p) return null;
  const frag = document.createDocumentFragment();
  frag.appendChild(_path(p));
  let range = "";
  if (detail.lines) range = String(detail.lines);
  else if (detail.offset !== undefined || detail.limit !== undefined) {
    const off = detail.offset ?? 0;
    const lim = detail.limit;
    range = lim !== undefined ? `lines ${off}–${off + lim}` : `from line ${off}`;
  }
  const chips = _chipRow([
    { label: "", value: range },
    { label: "pages", value: detail.pages },
  ]);
  if (chips) frag.appendChild(chips);
  return frag;
}

function _renderGlob(detail) {
  const pattern = detail.pattern;
  if (!pattern) return null;
  const frag = document.createDocumentFragment();
  frag.appendChild(_codeBlock(pattern));
  if (detail.path) frag.appendChild(_path(detail.path));
  return frag;
}

function _renderGrep(detail) {
  const pattern = detail.pattern;
  if (!pattern) return null;
  const frag = document.createDocumentFragment();
  frag.appendChild(_codeBlock(pattern));
  if (detail.path) frag.appendChild(_path(detail.path));
  const chips = _chipRow([
    { label: "glob", value: detail.glob },
    { label: "type", value: detail.type },
    { label: "include", value: detail.include },
    { label: "mode", value: detail.output_mode },
    { label: "-i", value: detail["-i"] ? "yes" : null },
    { label: "-n", value: detail["-n"] ? "yes" : null },
    { label: "-A", value: detail["-A"] },
    { label: "-B", value: detail["-B"] },
    { label: "-C", value: detail["-C"] ?? detail.context },
    { label: "multiline", value: detail.multiline ? "yes" : null },
  ]);
  if (chips) frag.appendChild(chips);
  return frag;
}

function _renderWeb(detail, toolName) {
  const target = detail.url || detail.query;
  if (!target) return null;
  const frag = document.createDocumentFragment();
  const isUrl = !!detail.url;
  frag.appendChild(_label(isUrl ? "url" : "query", "var(--fg-ghost)"));
  const big = document.createElement("div");
  big.className = "w-prominent";
  big.textContent = target;
  frag.appendChild(big);
  if (toolName.toLowerCase() === "webfetch" && detail.prompt) {
    frag.appendChild(_label("prompt", "var(--fg-ghost)"));
    frag.appendChild(_expandableValue(detail.prompt));
  }
  const chips = _chipRow([
    { label: "allowed_domains", value: Array.isArray(detail.allowed_domains) ? detail.allowed_domains.join(",") : detail.allowed_domains },
    { label: "blocked_domains", value: Array.isArray(detail.blocked_domains) ? detail.blocked_domains.join(",") : detail.blocked_domains },
  ]);
  if (chips) frag.appendChild(chips);
  return frag;
}

function _renderBrowser(detail, toolName) {
  const frag = document.createDocumentFragment();
  if (CDP_BROWSER_TOOLS.has(toolName.toLowerCase())) {
    const banner = document.createElement("div");
    banner.className = "w-chip-row";
    banner.appendChild(_cdpBanner());
    frag.appendChild(banner);
  }
  // Prominent line: most browser tools have ONE thing the user cares about.
  const primary = detail.url || detail.selector || detail.key || detail.text;
  if (primary !== undefined && primary !== null && primary !== "") {
    const labelText = detail.url ? "url" : detail.selector ? "selector" : detail.key ? "key" : "text";
    frag.appendChild(_label(labelText, "var(--fg-ghost)"));
    const big = document.createElement("div");
    big.className = "w-prominent";
    big.textContent = String(primary);
    frag.appendChild(big);
  }
  // Secondary fields as chips.
  const secondary = [];
  for (const [k, v] of Object.entries(detail)) {
    if (k === "tool_name" || k === "input") continue;
    if (k === "url" || k === "selector" || k === "key" || k === "text") continue;
    if (v === undefined || v === null || v === "") continue;
    if (k === "paths" && Array.isArray(v)) {
      for (const p of v) frag.appendChild(_path(p));
      continue;
    }
    const display = typeof v === "object" ? JSON.stringify(v) : String(v);
    secondary.push({ label: k, value: display.length > 80 ? display.slice(0, 80) + "…" : display });
  }
  const chips = _chipRow(secondary);
  if (chips) frag.appendChild(chips);
  return frag.childNodes.length ? frag : null;
}

function _renderGit(detail, toolName) {
  // git_* tools: render `git <subcommand> <args>` as a shell-style preview.
  // Subcommand falls back to the suffix of the tool name, e.g. `git_diff`.
  const sub = detail.subcommand || detail.command || toolName.replace(/^git[_-]?/i, "") || "";
  const argsParts = [];
  if (Array.isArray(detail.args)) argsParts.push(...detail.args.map(String));
  else if (typeof detail.args === "string") argsParts.push(detail.args);
  for (const k of ["ref", "branch", "path", "file", "message", "remote"]) {
    if (detail[k] !== undefined && detail[k] !== null && detail[k] !== "") argsParts.push(`${k}=${detail[k]}`);
  }
  const line = ["git", sub, ...argsParts].filter(Boolean).join(" ").trim();
  if (!line) return null;
  const frag = document.createDocumentFragment();
  frag.appendChild(_codeBlock(line));
  if (detail.cwd) {
    const chips = _chipRow([{ label: "cwd", value: detail.cwd }]);
    if (chips) frag.appendChild(chips);
  }
  return frag;
}

function _renderGenericTable(detail) {
  const table = document.createElement("table");
  for (const [k, v] of Object.entries(detail)) {
    if (k === "tool_name" || k === "input") continue;
    const row = document.createElement("tr");
    const keyCell = document.createElement("td");
    keyCell.textContent = k;
    const valCell = document.createElement("td");
    if (PATH_LIKE_KEYS.has(k.toLowerCase()) && typeof v === "string") {
      const span = document.createElement("span");
      span.className = "w-mono";
      span.textContent = v;
      valCell.appendChild(span);
    } else if (typeof v === "object" && v !== null) {
      const json = JSON.stringify(v, null, 2);
      if (json.length > 60) {
        const pre = document.createElement("pre");
        pre.className = "code w-inline-code";
        const code = document.createElement("code");
        code.textContent = _truncate(json, 2000);
        pre.appendChild(code);
        valCell.appendChild(pre);
      } else {
        valCell.textContent = json;
      }
    } else {
      valCell.appendChild(_expandableValue(String(v)));
    }
    row.append(keyCell, valCell);
    table.appendChild(row);
  }
  return table;
}

function renderRichDetail(detail, toolName) {
  const container = document.createElement("div");
  container.className = "w-detail-rich";
  const lower = (toolName || "").toLowerCase();

  let rendered = null;
  if (lower === "run_shell" || lower === "run_python3" || lower === "bash") {
    rendered = _renderShellLike(detail);
  } else if (lower === "write_text_file" || lower === "edit_text_file" || lower === "write" || lower === "edit") {
    rendered = _renderFileWrite(detail, toolName);
  } else if (lower === "read" || lower === "read_text_file") {
    rendered = _renderReadFile(detail);
  } else if (lower === "glob") {
    rendered = _renderGlob(detail);
  } else if (lower === "grep") {
    rendered = _renderGrep(detail);
  } else if (lower === "webfetch" || lower === "websearch" || lower === "web_fetch" || lower === "web_search") {
    rendered = _renderWeb(detail, toolName);
  } else if (lower.startsWith("browser_")) {
    rendered = _renderBrowser(detail, toolName);
  } else if (lower.startsWith("git_") || lower.startsWith("git-")) {
    rendered = _renderGit(detail, toolName);
  }

  if (rendered) {
    container.appendChild(rendered);
    return container;
  }

  // Fallback: pretty key/value table with path styling, JSON pretty-print
  // for long objects, and a "show more" toggle for long strings.
  container.appendChild(_renderGenericTable(detail));
  return container;
}

// ---------------------------------------------------------------------------
// Per-tool permission persistence (Feature 2)

async function getToolPerm(toolName) {
  if (!toolName) return null;
  try {
    const store = await chrome.storage.local.get(TOOL_PERMS_KEY);
    const perms = store[TOOL_PERMS_KEY] || {};
    return perms[toolName] || null;
  } catch { return null; }
}

async function setToolPerm(toolName, perm) {
  if (!toolName) return;
  try {
    const store = await chrome.storage.local.get(TOOL_PERMS_KEY);
    const perms = store[TOOL_PERMS_KEY] || {};
    perms[toolName] = perm;
    await chrome.storage.local.set({ [TOOL_PERMS_KEY]: perms });
  } catch {}
}

async function clearToolPerms() {
  try {
    await chrome.storage.local.remove(TOOL_PERMS_KEY);
  } catch {}
}

// ---------------------------------------------------------------------------
// Widgets

function createBaseWidget(kind, question, actions) {
  const wrap = document.createElement("div");
  wrap.className = `msg widget widget-${kind}`;
  const r = document.createElement("div");
  r.className = "role";
  r.textContent = "?";
  const body = document.createElement("div");
  body.className = "body";

  const questionEl = document.createElement("div");
  questionEl.className = "w-question";
  questionEl.textContent = question;
  body.appendChild(questionEl);

  const actionsRow = document.createElement("div");
  actionsRow.className = "w-actions";
  for (const { label, className, dataset, onClick } of actions) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = className || "w-btn";
    if (dataset) Object.assign(btn.dataset, dataset);
    btn.textContent = label;
    btn.addEventListener("click", onClick);
    actionsRow.appendChild(btn);
  }

  wrap.append(r, body);
  return { wrap, body, questionEl, actionsRow };
}

function renderWidget(msg) {
  // Plan approval widget
  if (msg.kind === "plan_approval") {
    renderPlanApprovalWidget(msg);
    return;
  }

  const widgetToolName = (msg.kind === "tool_confirm" && msg.detail?.tool_name) ? msg.detail.tool_name : "";
  const actionList = Array.isArray(msg.actions) && msg.actions.length > 0 ? msg.actions : ["ok"];
  const actionDefs = actionList.map((action) => ({
    label: action.replace(/_/g, " "),
    className: "w-btn" + (action === msg.default ? " default" : ""),
    dataset: { action },
    onClick: () => resolveWidget(msg.id, action, wrap, "", widgetToolName),
  }));

  const { wrap, body, actionsRow } = createBaseWidget(msg.kind || "confirm", msg.question || "(no question)", actionDefs);

  if (msg.detail) {
    // Rich detail for tool_confirm
    if (msg.kind === "tool_confirm" && typeof msg.detail === "object") {
      const toolName = msg.detail.tool_name || "";
      const toolInput = msg.detail.input || msg.detail;
      body.insertBefore(renderRichDetail(toolInput, toolName), actionsRow);
    } else {
      const det = document.createElement("pre");
      det.className = "w-detail";
      try {
        det.textContent = JSON.stringify(msg.detail, null, 2);
      } catch {
        det.textContent = String(msg.detail);
      }
      body.insertBefore(det, actionsRow);
    }
  }

  let textInput = null;
  if (msg.kind === "question") {
    textInput = document.createElement("input");
    textInput.type = "text";
    textInput.placeholder = "reply…";
    textInput.className = "w-text";
    textInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        resolveWidget(msg.id, "reply", wrap, textInput.value, widgetToolName);
      }
    });
    body.insertBefore(textInput, actionsRow);
  }

  body.appendChild(actionsRow);
  log.appendChild(wrap);
  scrollToBottom();

  (textInput || actionsRow.querySelector(".default") || actionsRow.querySelector(".w-btn"))?.focus();
}

function renderPlanApprovalWidget(msg) {
  const actionDefs = [
    {
      label: "approve",
      className: "w-btn default",
      dataset: { action: "approve" },
      onClick: () => resolveWidget(msg.id, "approve", wrap),
    },
    {
      label: "reject",
      className: "w-btn",
      dataset: { action: "reject" },
      onClick: () => resolveWidget(msg.id, "reject", wrap),
    },
    {
      label: "modify",
      className: "w-btn",
      dataset: { action: "modify" },
      onClick: () => { modifyWrap.hidden = !modifyWrap.hidden; if (!modifyWrap.hidden) modifyInput.focus(); },
    },
  ];

  const { wrap, body, actionsRow } = createBaseWidget("plan_approval", msg.question || "Plan approval requested", actionDefs);

  // Plan text block
  const planBlock = document.createElement("div");
  planBlock.className = "w-plan-block";
  planBlock.textContent = msg.plan_text || msg.detail?.plan_text || msg.text || "(no plan)";
  body.insertBefore(planBlock, actionsRow);

  // Modify input (hidden by default)
  const modifyWrap = document.createElement("div");
  modifyWrap.className = "w-plan-modify";
  modifyWrap.hidden = true;
  const modifyInput = document.createElement("input");
  modifyInput.type = "text";
  modifyInput.placeholder = "modifications…";
  modifyInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      resolveWidget(msg.id, "modify", wrap, modifyInput.value);
    }
  });
  modifyWrap.appendChild(modifyInput);
  body.insertBefore(modifyWrap, actionsRow);

  body.appendChild(actionsRow);
  log.appendChild(wrap);
  scrollToBottom();
  actionsRow.querySelector(".default")?.focus();
}

function resolveWidget(widgetId, action, bubbleEl, text = "", toolName = "") {
  try {
    port?.postMessage({
      type: MsgType.WIDGET_RESPONSE,
      widget_id: widgetId,
      action,
      text,
    });
  } catch {}
  // Persist always_allow pref
  if (action === "always_allow" && toolName) {
    setToolPerm(toolName, "always_allow");
  }
  for (const b of bubbleEl.querySelectorAll(".w-btn")) {
    b.disabled = true;
    if (b.dataset.action === action) b.classList.add("chosen");
  }
  const ti = bubbleEl.querySelector(".w-text");
  if (ti) ti.disabled = true;
  const pi = bubbleEl.querySelector(".w-plan-modify input");
  if (pi) pi.disabled = true;
}

// ---------------------------------------------------------------------------
// Error with trace

function renderErrorWithTrace(bubble, message, trace) {
  bubble.textContent = message;
  if (trace) {
    const link = document.createElement("span");
    link.className = "error-trace-link";
    link.textContent = "show trace";
    let expanded = false;
    let traceEl = null;
    link.addEventListener("click", () => {
      if (!expanded) {
        traceEl = document.createElement("pre");
        traceEl.className = "error-trace";
        traceEl.textContent = trace;
        bubble.appendChild(traceEl);
        link.textContent = "hide trace";
        expanded = true;
      } else {
        traceEl?.remove();
        link.textContent = "show trace";
        expanded = false;
      }
      scrollToBottom();
    });
    bubble.appendChild(document.createElement("br"));
    bubble.appendChild(link);
  }
}

// ---------------------------------------------------------------------------
// Browser-tool executor — runs chrome.scripting / chrome.tabs ops on behalf
// of the native host, then posts the result back over the port.

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

async function handleBrowserTool(msg) {
  const { id: reqId, op, args = {} } = msg;
  try {
    const result = await runBrowserOp(op, args);
    port?.postMessage({ type: "browser-tool-response", id: reqId, ok: true, result });
  } catch (err) {
    port?.postMessage({
      type: "browser-tool-response",
      id: reqId,
      ok: false,
      error: String(err?.message ?? err),
    });
  }
}

// ---------------------------------------------------------------------------
// Chrome DevTools Protocol bridge.
//
// Synthesised KeyboardEvents / MouseEvents from `chrome.scripting` have
// isTrusted=false, so they can't drive the browser's own input handlers
// (Tab focus motion, characters appearing in inputs, drag-and-drop, file
// pickers). The CDP path attaches the extension as a debugger to the active
// tab and routes input through `Input.dispatchKeyEvent` / `dispatchMouseEvent`
// — same protocol Puppeteer/Playwright use, so isTrusted=true.
//
// Cost: a yellow banner appears on the tab while attached. We attach lazily
// (only when a CDP-backed tool is invoked) and stay attached for the life
// of the panel; the model can call browser_cdp_detach to dismiss it.

const cdpState = {
  attached: new Set(),
  consoleLogs: new Map(),  // tabId -> [{level, text, ts}]
  networkLog: new Map(),   // tabId -> [{requestId, method, url, status, mime, ts}]
};

const CDP_LOG_LIMIT = 250;

function _cdpModifiers(modifiers) {
  // CDP modifiers bitfield: 1=Alt, 2=Ctrl, 4=Meta, 8=Shift.
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
  return key;  // "Enter", "Escape", etc. match KeyboardEvent.code names.
}

async function ensureCdpAttached(tabId) {
  if (cdpState.attached.has(tabId)) return;
  await chrome.debugger.attach({ tabId }, "1.3");
  cdpState.attached.add(tabId);
  cdpState.consoleLogs.set(tabId, []);
  cdpState.networkLog.set(tabId, []);
  // Enable the domains we read from. Network adds slight overhead (every
  // request fires events) but is essential for `browser_network_log`.
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

        // Native input / textarea: use the prototype's value setter so React,
        // Vue, Svelte, etc. observe the change.
        const isInput = el.tagName === "INPUT" || el.tagName === "TEXTAREA";
        if (isInput) {
          const proto = el.tagName === "TEXTAREA"
            ? HTMLTextAreaElement.prototype
            : HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
          if (setter) setter.call(el, val); else el.value = val;
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
          return { ok: true, kind: "input" };
        }

        // contenteditable host (Notion, Google Docs, Linear's editor, ProseMirror,
        // Slate, Lexical). Walk up to find the editable root; rich-text editors
        // route InputEvents from any descendant up to the host.
        let host = el;
        while (host && host !== document.body) {
          if (host.isContentEditable) break;
          host = host.parentElement;
        }
        if (!host || !host.isContentEditable) {
          return {
            ok: false,
            error: "selector matched a non-input, non-contenteditable element",
          };
        }

        host.focus();
        // Select existing content so insertText replaces (matches user paste).
        const range = document.createRange();
        range.selectNodeContents(host);
        const selObj = window.getSelection();
        selObj?.removeAllRanges();
        selObj?.addRange(range);

        // Dispatch a beforeinput so editors that gate on it (ProseMirror, Lexical,
        // Slate) accept the change. Then fall back to execCommand("insertText")
        // which most rich-text editors translate into their internal model
        // mutations.
        const inputEvent = new InputEvent("beforeinput", {
          bubbles: true,
          cancelable: true,
          inputType: "insertReplacementText",
          data: val,
        });
        host.dispatchEvent(inputEvent);

        if (!inputEvent.defaultPrevented) {
          // execCommand is deprecated but still the most reliable cross-editor
          // path. Editors that don't support it can listen on `beforeinput`
          // (above) and apply the change themselves.
          document.execCommand("insertText", false, val);
        }

        host.dispatchEvent(new InputEvent("input", {
          bubbles: true,
          inputType: "insertReplacementText",
          data: val,
        }));
        return { ok: true, kind: "contenteditable" };
      }, [selector, value]);
    }
    case "eval_js": {
      const { expression } = args;
      // Wrap in IIFE so expressions with `return` work.
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
      // Synthesised KeyboardEvents have isTrusted=false. Browser-default
      // behaviours that hang off real keypresses (Tab moving focus, Escape
      // closing native dialogs, typing into inputs) won't fire — but app-level
      // listeners (most "press / to search", "Cmd-K palette", form submit on
      // Enter, custom modal close on Escape) work fine.
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
      // Run in the side-panel context (extension page), where the
      // clipboardRead permission is granted unconditionally. Reading via the
      // tab's content script would require transient activation per page.
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
      // Real native typing — characters actually appear in the focused input,
      // including in cross-origin iframes. isTrusted=true.
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
      // CDP keyboard event. Tab moves focus, arrow keys navigate, Enter
      // submits — all the browser-default behaviours synthesised events
      // can't trigger.
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
      // Single printable char: also send a "char" event so the character
      // shows up in inputs without needing Input.insertText.
      if (key.length === 1 && (mods & 6) === 0) {  // no Ctrl/Meta
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
      // Real mouse: hover (mouseMoved) + press + release. Hover-dependent
      // UI like dropdown menus and tooltips actually shows. Drag-drop
      // is implemented separately if needed.
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
      // Sets files on an <input type="file"> via DOM.setFileInputFiles, the
      // only path that works without OS-level UI automation. ``paths`` are
      // absolute paths on the user's machine — both processes are local so
      // the browser can read them.
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
      // Lets the model dismiss the yellow banner once it's done with
      // CDP-backed work. Idempotent.
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

// ---------------------------------------------------------------------------
// Composer autosize

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px";
}
input.addEventListener("input", () => {
  autosize();
  updateAutocomplete();
});

// ---------------------------------------------------------------------------
// Drag-drop and paste for images/files

const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];

function setupDragDrop() {
  const zones = [form, log];
  for (const zone of zones) {
    zone.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      zone.classList.add("drop-zone-active");
    });
    zone.addEventListener("dragleave", (e) => {
      // Only remove if leaving the element itself
      if (e.relatedTarget && zone.contains(e.relatedTarget)) return;
      zone.classList.remove("drop-zone-active");
    });
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      zone.classList.remove("drop-zone-active");
      handleDroppedFiles(e.dataTransfer.files);
    });
  }

  // Paste handler on textarea
  input.addEventListener("paste", (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (IMAGE_TYPES.includes(item.type)) {
        e.preventDefault();
        const file = item.getAsFile();
        if (file) handleDroppedFiles([file]);
        return;
      }
    }
  });
}

function handleDroppedFiles(fileList) {
  for (const file of fileList) {
    if (IMAGE_TYPES.includes(file.type)) {
      const reader = new FileReader();
      reader.onload = () => {
        pendingImages.push({ dataUrl: reader.result, name: file.name });
        renderDropPreview();
      };
      reader.readAsDataURL(file);
    } else if (file.type.startsWith("text/") || file.name.match(/\.(txt|md|json|yaml|yml|toml|csv|tsv|xml|html|css|js|ts|py|rs|go|java|c|cpp|h|rb|sh|sql)$/i)) {
      const reader = new FileReader();
      reader.onload = () => {
        pendingTextFiles.push({ name: file.name, content: reader.result });
        renderDropPreview();
      };
      reader.readAsText(file);
    }
  }
}

function renderDropPreview() {
  dropPreview.innerHTML = "";
  const hasItems = pendingImages.length > 0 || pendingTextFiles.length > 0;
  dropPreview.hidden = !hasItems;

  for (let i = 0; i < pendingImages.length; i++) {
    const item = pendingImages[i];
    const el = document.createElement("div");
    el.className = "drop-preview-item";
    const img = document.createElement("img");
    img.src = item.dataUrl;
    const name = document.createElement("span");
    name.textContent = item.name;
    const remove = document.createElement("span");
    remove.className = "drop-remove";
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      pendingImages.splice(i, 1);
      renderDropPreview();
    });
    el.append(img, name, remove);
    dropPreview.appendChild(el);
  }

  for (let i = 0; i < pendingTextFiles.length; i++) {
    const item = pendingTextFiles[i];
    const el = document.createElement("div");
    el.className = "drop-preview-item";
    const name = document.createElement("span");
    name.textContent = item.name;
    const remove = document.createElement("span");
    remove.className = "drop-remove";
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      pendingTextFiles.splice(i, 1);
      renderDropPreview();
    });
    el.append(name, remove);
    dropPreview.appendChild(el);
  }
}

setupDragDrop();

// ---------------------------------------------------------------------------
// Page context

async function getPageContext() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) return {};
    try {
      const res = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          const trim = (s, n) => (s.length > n ? s.slice(0, n) + "\n…[truncated]" : s);
          const sel = window.getSelection()?.toString() || "";
          return {
            url: location.href,
            title: document.title,
            selection: trim(sel, 20000),
            text: trim(document.body?.innerText ?? "", 15000),
            headings: [...document.querySelectorAll("h1,h2,h3")]
              .slice(0, 40)
              .map((h) => ({
                level: h.tagName.toLowerCase(),
                text: (h.innerText || "").trim().slice(0, 160),
              })),
          };
        },
      });
      return res?.[0]?.result ?? { url: tab.url || "", title: tab.title || "" };
    } catch {
      return { url: tab.url || "", title: tab.title || "" };
    }
  } catch {
    return {};
  }
}

// ---------------------------------------------------------------------------
// KAIROS toggle

sbKairos.addEventListener("click", () => {
  const newState = kairosState === "on" ? "off" : "on";
  try {
    port?.postMessage({ type: MsgType.KAIROS, action: newState });
  } catch {}
});

// ---------------------------------------------------------------------------
// Checkpoint / rewind

function showCheckpointModal() {
  checkpointModal.classList.remove("hidden");
  checkpointNameInput.value = "";
  checkpointList.innerHTML = '<div class="checkpoint-empty">loading…</div>';
  // Send /checkpoint list
  const id = crypto.randomUUID?.() ?? `cp-${Date.now()}`;
  checkpointPendingId = id;
  // Suppress default message rendering by not adding a visible bubble
  const bubble = document.createElement("div");
  pending.set(id, { bubble, streamedText: "", silent: true });
  try {
    port?.postMessage({ type: MsgType.COMMAND, id, raw: "/checkpoint list", backend: backendSel.value });
  } catch {}
}

function hideCheckpointModal() {
  checkpointModal.classList.add("hidden");
  checkpointPendingId = null;
}

// ---------- Recent sessions modal ----------

function showRecentSessionsModal() {
  recentSessionsModal.classList.remove("hidden");
  recentSessionsList.innerHTML = '<div class="recent-sessions-empty">Loading…</div>';
  const id = crypto.randomUUID?.() ?? `rs-${Date.now()}`;
  recentSessionsRequestId = id;
  if (recentSessionsTimeout) clearTimeout(recentSessionsTimeout);
  recentSessionsTimeout = setTimeout(() => {
    if (recentSessionsRequestId !== id) return;
    recentSessionsList.innerHTML =
      '<div class="recent-sessions-empty">Could not load sessions (timeout).</div>';
    recentSessionsRequestId = null;
  }, 5000);
  try {
    port?.postMessage({ type: MsgType.LIST_SESSIONS, id });
  } catch {}
}

function hideRecentSessionsModal() {
  recentSessionsModal.classList.add("hidden");
  recentSessionsRequestId = null;
  if (recentSessionsTimeout) {
    clearTimeout(recentSessionsTimeout);
    recentSessionsTimeout = null;
  }
}

function relativeTime(iso) {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const sec = Math.max(1, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

function renderRecentSessions(sessions) {
  recentSessionsList.innerHTML = "";
  if (!sessions.length) {
    const empty = document.createElement("div");
    empty.className = "recent-sessions-empty";
    empty.textContent = "No saved sessions.";
    recentSessionsList.appendChild(empty);
    return;
  }
  for (const s of sessions) {
    const row = document.createElement("div");
    row.className = "recent-session-row";
    row.dataset.sid = s.session_id || "";

    const title = document.createElement("div");
    title.className = "recent-session-title";
    const summary = (s.summary || "").trim();
    title.textContent = summary
      ? (summary.length > 60 ? summary.slice(0, 60) + "…" : summary)
      : (s.session_id || "(no title)");

    const meta = document.createElement("div");
    meta.className = "recent-session-meta";
    const parts = [];
    if (s.backend) parts.push(s.backend);
    if (s.message_count) parts.push(`${s.message_count} msgs`);
    const rel = relativeTime(s.created);
    if (rel) parts.push(rel);
    meta.textContent = parts.join(" · ");

    row.appendChild(title);
    row.appendChild(meta);
    row.addEventListener("click", () => {
      const sid = row.dataset.sid;
      if (!sid) return;
      hideRecentSessionsModal();
      const id = crypto.randomUUID?.() ?? `r-${Date.now()}`;
      try {
        port?.postMessage({
          type: MsgType.COMMAND,
          id,
          raw: `/resume ${sid}`,
          backend: backendSel.value,
        });
      } catch {}
    });
    recentSessionsList.appendChild(row);
  }
}

recentSessionsBtn?.addEventListener("click", showRecentSessionsModal);
recentSessionsClose?.addEventListener("click", hideRecentSessionsModal);
recentSessionsModal?.addEventListener("click", (e) => {
  if (e.target === recentSessionsModal) hideRecentSessionsModal();
});

function parseCheckpointList(text) {
  // Extract checkpoint names from lines like "  · name" or "name" or "- name"
  const names = [];
  for (const line of text.split("\n")) {
    const m = /^\s*[·\-\*]?\s*([^\s][^\n]+)$/.exec(line.trim());
    if (m && m[1] && !m[1].startsWith("checkpoint") && !m[1].startsWith("No ") && !m[1].startsWith("Usage")) {
      names.push(m[1].trim());
    }
  }
  return names;
}

function renderCheckpointList(text) {
  checkpointList.innerHTML = "";
  const names = parseCheckpointList(text);
  if (names.length === 0) {
    const empty = document.createElement("div");
    empty.className = "checkpoint-empty";
    empty.textContent = "no checkpoints saved";
    checkpointList.appendChild(empty);
    return;
  }
  for (const name of names) {
    const row = document.createElement("div");
    row.className = "checkpoint-row";
    const nameEl = document.createElement("span");
    nameEl.className = "cp-name";
    nameEl.textContent = name;
    const restoreBtn = document.createElement("button");
    restoreBtn.className = "cp-restore";
    restoreBtn.textContent = "restore";
    restoreBtn.addEventListener("click", () => {
      hideCheckpointModal();
      sendSilentCommand(`/checkpoint restore ${name}`);
    });
    row.append(nameEl, restoreBtn);
    checkpointList.appendChild(row);
  }
}

function sendSilentCommand(raw) {
  const id = crypto.randomUUID?.() ?? `sc-${Date.now()}`;
  const bubble = document.createElement("div");
  pending.set(id, { bubble, streamedText: "", silent: true });
  try {
    port?.postMessage({ type: MsgType.COMMAND, id, raw, backend: backendSel.value });
  } catch {}
}

sbRewind.addEventListener("click", showCheckpointModal);

checkpointClose.addEventListener("click", hideCheckpointModal);
checkpointModal.addEventListener("click", (e) => {
  if (e.target === checkpointModal) hideCheckpointModal();
});

checkpointSaveBtn.addEventListener("click", () => {
  const name = checkpointNameInput.value.trim();
  if (!name) return;
  sendSilentCommand(`/checkpoint save ${name}`);
  checkpointNameInput.value = "";
  // Refresh list after a short delay
  setTimeout(() => {
    const id = crypto.randomUUID?.() ?? `cp-${Date.now()}`;
    checkpointPendingId = id;
    checkpointList.innerHTML = '<div class="checkpoint-empty">loading…</div>';
    const bubble = document.createElement("div");
    pending.set(id, { bubble, streamedText: "", silent: true });
    try {
      port?.postMessage({ type: MsgType.COMMAND, id, raw: "/checkpoint list", backend: backendSel.value });
    } catch {}
  }, 600);
});

checkpointNameInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); checkpointSaveBtn.click(); }
  if (e.key === "Escape") { e.preventDefault(); hideCheckpointModal(); }
});

// ---------------------------------------------------------------------------
// Disconnect / crash recovery banner

let disconnectBanner = null;
let reconnectTimer = null;

function showDisconnectBanner() {
  if (disconnectBanner) return;
  disconnectBanner = document.createElement("div");
  disconnectBanner.className = "disconnect-banner";

  const text = document.createElement("span");
  text.className = "banner-text";
  text.textContent = "Connection lost";

  const reconnectBtn = document.createElement("button");
  reconnectBtn.textContent = "Reconnect";
  reconnectBtn.addEventListener("click", () => {
    hideDisconnectBanner();
    connect();
  });

  const diagnoseBtn = document.createElement("button");
  diagnoseBtn.textContent = "Diagnose";
  diagnoseBtn.addEventListener("click", () => {
    try {
      port?.postMessage({ type: MsgType.DIAG, id: crypto.randomUUID?.() ?? `diag-${Date.now()}` });
    } catch {}
  });

  disconnectBanner.append(text, reconnectBtn, diagnoseBtn);
  log.insertBefore(disconnectBanner, log.firstChild);

  // Auto-reconnect after 3 seconds
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (!port) {
      hideDisconnectBanner();
      connect();
    }
  }, 3000);
}

function hideDisconnectBanner() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (disconnectBanner) { disconnectBanner.remove(); disconnectBanner = null; }
}

// ---------------------------------------------------------------------------
// Port wiring

/**
 * Wrap ``port.postMessage`` so every outbound frame is tagged with
 * ``profile_id`` without touching each call site. The raw Chrome port
 * is not reused anywhere else, so this is safe.
 */
function wrapPortForProfileId(p) {
  const origPost = p.postMessage.bind(p);
  p.postMessage = (msg) => {
    try {
      return origPost(tagMessage(msg));
    } catch (err) {
      // Fall back to the untagged message if tagging threw — never break
      // the wire because of instrumentation.
      return origPost(msg);
    }
  };
}

function connect() {
  hideDisconnectBanner();
  port = chrome.runtime.connect({ name: "sidepanel" });
  wrapPortForProfileId(port);

  port.onMessage.addListener((msg) => {
    if (!msg || typeof msg !== "object") return;
    switch (msg.type) {
      case MsgType.BRIDGE_READY:
        status.textContent = "";
        break;
      case "tab_context":
        // Background tells us which Chrome tab this panel is associated with.
        chromeTabId = msg.tabId ?? chromeTabId;
        loadTabState();
        break;
      case "tab_switched":
        // User switched to a different Chrome tab — swap the conversation.
        if (msg.tabId !== chromeTabId) {
          saveTabState().then(() => {
            chromeTabId = msg.tabId;
            loadTabState();
          });
        }
        break;
      case MsgType.READY:
        updateStatusbar(msg);
        if (Array.isArray(msg.commands)) commandIndex = msg.commands;
        if (Array.isArray(msg.skills)) skillIndex = msg.skills;
        if (Array.isArray(msg.at_commands)) atCommandIndex = msg.at_commands;
        // Populate workspace picker
        if (Array.isArray(msg.workspaces)) {
          workspaceSel.innerHTML = '<option value="">(none)</option>';
          for (const ws of msg.workspaces) {
            const opt = document.createElement("option");
            opt.value = ws;
            opt.textContent = ws;
            workspaceSel.appendChild(opt);
          }
        }
        if (!log.querySelector(".msg")) {
          addMessage(
            "system",
            `connected · obscura ${msg.version ? `v${msg.version}` : ""}` +
              `${msg.python_version ? ` · py${msg.python_version}` : ""}` +
              `${msg.git_commit ? ` · ${msg.git_commit}` : ""}`,
          );
        }
        break;
      case MsgType.THINKING: {
        setLiveStatus("thinking…");
        // Accumulate thinking text
        if (msg.id && msg.text) {
          const thinkEl = ensureThinkingBlock(msg.id);
          if (thinkEl) {
            const st = pending.get(msg.id);
            st.thinkingText = (st.thinkingText || "") + msg.text;
            thinkEl.textContent = st.thinkingText;
            scrollToBottom();
          }
        }
        break;
      }
      case "resolved": {
        // $skills / @command breadcrumb resolved on the host.
        const tokens = Array.isArray(msg.tokens) ? msg.tokens : [];
        if (tokens.length) {
          const bar = document.createElement("div");
          bar.className = "resolved-bar";
          bar.textContent = "resolved " + tokens.join("  ");
          const st = pending.get(msg.id);
          if (st?.bubble?.parentElement) {
            st.bubble.parentElement.parentElement?.insertBefore(bar, st.bubble.parentElement);
          } else {
            log.appendChild(bar);
          }
          scrollToBottom();
        }
        break;
      }
      case MsgType.KAIROS: {
        // KAIROS state update from host
        if (msg.state === "on" || msg.state === "already_on") {
          kairosState = "on";
          sbKairos.classList.add("on");
        } else {
          kairosState = "off";
          sbKairos.classList.remove("on");
        }
        break;
      }
      case MsgType.TOOL_START:
        toolStart(msg.id, msg.tool_use_id, msg.tool_name);
        break;
      case MsgType.TOOL_DELTA:
        toolDelta(msg.id, msg.tool_use_id, msg.delta || "");
        break;
      case MsgType.TOOL_END:
        toolEnd(msg.id, msg.tool_use_id);
        setLiveStatus("thinking…");
        break;
      case MsgType.TOOL_RESULT:
        toolResult(msg.id, msg.tool_use_id, msg.text || "");
        break;
      case MsgType.CHUNK: {
        const st = pending.get(msg.id);
        if (!st) return;
        st.streamedText = (st.streamedText || "") + (msg.text ?? "");
        renderMarkdown(st.bubble, st.streamedText);
        st.bubble.parentElement?.classList.add("cursor");
        scrollToBottom();
        break;
      }
      case MsgType.DONE: {
        const st = pending.get(msg.id);
        // Handle special silent command responses
        if (st?.silent) {
          const text = st.streamedText || "";
          if (msg.id === checkpointPendingId) {
            renderCheckpointList(text);
            checkpointPendingId = null;
          } else if (msg.id === fleetPendingId) {
            renderFleetContent(null, text);
            fleetPendingId = null;
          } else if (msg.id === mcpPendingId) {
            renderMcpContent(text);
            mcpPendingId = null;
          }
          pending.delete(msg.id);
          if (pending.size === 0) { setBusy(false); setLiveStatus(""); drainQueue(); }
          break;
        }
        if (st) {
          st.bubble.parentElement?.classList.remove("cursor");
          if (st.streamedText) {
            renderMarkdown(st.bubble, st.streamedText);
          } else {
            // No text was streamed — inject a subtle ack so the bubble isn't blank.
            const ack = document.createElement("span");
            ack.className = "cmd-ack";
            ack.textContent = "✓";
            st.bubble.appendChild(ack);
          }
          scheduleTranscriptSave();
        }
        pending.delete(msg.id);
        // Only adopt session_id if we actually sent this request. Otherwise a
        // stray broadcast from another tab's conversation can clobber ours.
        if (msg.session_id && st) {
          sessionId = msg.session_id;
          // Update tab label if it's the first message
          if (tabManager.active && tabManager.active.label === "session") {
            const firstUser = log.querySelector(".msg.user .body");
            if (firstUser) {
              tabManager.active.label = (firstUser.textContent || "").slice(0, 20) || "session";
              renderTabs();
              saveTabs();
            }
          }
          // Save session metadata
          const firstUser = log.querySelector(".msg.user .body");
          saveSessionMeta(sessionId, firstUser?.textContent || "");
        }
        if (pending.size === 0) { setBusy(false); setLiveStatus(""); drainQueue(); }
        break;
      }
      case MsgType.WIDGET: {
        // For tool_confirm, check if the user has an always_allow perm stored.
        if (msg.kind === "tool_confirm") {
          const toolName = msg.detail?.tool_name || "";
          getToolPerm(toolName).then((perm) => {
            if (perm === "always_allow") {
              // Auto-approve without showing widget
              try {
                port?.postMessage({
                  type: MsgType.WIDGET_RESPONSE,
                  widget_id: msg.id,
                  action: "allow",
                  text: "",
                });
              } catch {}
            } else {
              renderWidget(msg);
            }
          });
        } else {
          renderWidget(msg);
        }
        break;
      }
      case MsgType.DIAG: {
        // If the fleet overlay is open and waiting for diag data, populate it
        if (!fleetOverlay.classList.contains("hidden") && fleetPendingId === null) {
          renderFleetContent(msg, null);
        } else {
          showDiagOverlay(msg);
        }
        break;
      }
      case MsgType.WARNING: {
        showWarningBanner(msg.message || "Warning");
        break;
      }
      case "auth_required": {
        showAuthGate();
        break;
      }
      case MsgType.SESSIONS: {
        // Reply to our list_sessions probe — render the modal list.
        if (msg.id !== recentSessionsRequestId) break;
        if (recentSessionsTimeout) {
          clearTimeout(recentSessionsTimeout);
          recentSessionsTimeout = null;
        }
        renderRecentSessions(Array.isArray(msg.sessions) ? msg.sessions : []);
        break;
      }
      case "browser-tool": {
        handleBrowserTool(msg);
        break;
      }
      case MsgType.ERROR: {
        const st = msg.id ? pending.get(msg.id) : null;
        if (st?.bubble) {
          const bubble = st.bubble;
          bubble.parentElement?.classList.remove("cursor");
          bubble.parentElement.classList.remove("assistant");
          bubble.parentElement.classList.add("error");
          renderErrorWithTrace(bubble, msg.message || "Unknown error", msg.trace);
        } else {
          const body = addMessage("error", "");
          renderErrorWithTrace(body, msg.message || "Unknown error", msg.trace);
        }
        if (msg.id) pending.delete(msg.id);
        if (pending.size === 0) { setBusy(false); setLiveStatus(""); drainQueue(); }
        break;
      }
    }
  });

  port.onDisconnect.addListener(() => {
    setBusy(false);
    setLiveStatus("");
    port = null;
    setHostStatus("disconnected", "err");
    showDisconnectBanner();
  });

  // Kick the service worker to spawn the native host. Without this the SW
  // only spawns the host on the first outgoing message, so the initial
  // `ready` frame (which carries commands / skills / workspaces / pid) never
  // arrives until the user types something.
  try {
    port.postMessage({ type: MsgType.PING, id: "boot" });
  } catch {}
}

// ---------------------------------------------------------------------------
// Statusbar

function setHostStatus(text, level = "warn") {
  sbHost.textContent = `host: ${text}`;
  sbHost.classList.remove("ok", "warn", "err");
  if (level) sbHost.classList.add(level);
}

function updateStatusbar(ready) {
  const bits = [];
  if (ready.version) bits.push(`v${ready.version}`);
  if (ready.python_version) bits.push(`py${ready.python_version}`);
  if (ready.pid) bits.push(`pid ${ready.pid}`);
  setHostStatus(bits.join(" · ") || "up", "ok");
  sbGit.textContent = ready.git_commit ? `@${ready.git_commit}` : "—";
}

function updateBackendStatus() {
  sbBackend.textContent = backendSel.value;
}
backendSel.addEventListener("change", updateBackendStatus);
updateBackendStatus();
setHostStatus("connecting…", "warn");

// ---------------------------------------------------------------------------
// Slash-command autocomplete

const autocomplete = document.createElement("div");
autocomplete.id = "autocomplete";
autocomplete.className = "autocomplete";
autocomplete.hidden = true;
form.appendChild(autocomplete);

let acItems = [];      // [{ label, insert, doc?, prefix }]
let acIndex = 0;
let acTooltip = null;  // tooltip element

/**
 * Determine the token at the cursor that should drive autocomplete.
 * Returns `{ prefix, query, startOffset, endOffset }` for the nearest
 * ``/``, ``$`` or ``@`` token, or ``null`` if none.
 *
 * This lets the user type "$python @review explain foo" and get suggestions
 * for each token as they type it.
 */
function currentTriggerToken() {
  const pos = input.selectionStart ?? input.value.length;
  const before = input.value.slice(0, pos);
  // Find the last trigger that isn't followed by whitespace before the cursor.
  const match = /(^|\s)([/$@])([^\s/$@]*)$/.exec(before);
  if (!match) return null;
  const prefix = match[2];
  const query = match[3];
  const startOffset = pos - query.length - 1;
  return { prefix, query, startOffset, endOffset: pos };
}

function updateAutocomplete() {
  const tok = currentTriggerToken();
  if (!tok) {
    autocomplete.hidden = true;
    hideAcTooltip();
    return;
  }
  const q = tok.query.toLowerCase();
  let items = [];

  if (tok.prefix === "/") {
    items = commandIndex
      .filter((c) => c.name.toLowerCase().startsWith(q))
      .slice(0, 10)
      .map((c) => ({
        label: "/" + c.name,
        insert: "/" + c.name + " ",
        doc: c.doc || "",
        fullDoc: c.doc || "",
        prefix: "/",
      }));
  } else if (tok.prefix === "$") {
    items = skillIndex
      .filter((name) => name.toLowerCase().startsWith(q))
      .slice(0, 12)
      .map((name) => ({
        label: "$" + name,
        insert: "$" + name + " ",
        doc: "inject skill into prompt",
        fullDoc: "inject skill into prompt",
        prefix: "$",
      }));
  } else if (tok.prefix === "@") {
    items = atCommandIndex
      .filter((name) => name.toLowerCase().startsWith(q))
      .slice(0, 12)
      .map((name) => ({
        label: "@" + name,
        insert: "@" + name + " ",
        doc: "user-defined command",
        fullDoc: "user-defined command",
        prefix: "@",
      }));
  }

  acItems = items;
  if (!acItems.length) {
    autocomplete.hidden = true;
    hideAcTooltip();
    return;
  }
  acIndex = 0;
  renderAutocomplete();
}

function renderAutocomplete() {
  autocomplete.innerHTML = "";
  acItems.forEach((it, i) => {
    const el = document.createElement("div");
    el.className = "ac-item" + (i === acIndex ? " active" : "");
    el.dataset.prefix = it.prefix;
    const truncDoc = it.doc && it.doc.length > 40 ? it.doc.slice(0, 40) + "…" : it.doc;
    el.innerHTML =
      `<span class="ac-name">${escHtml(it.label)}</span>` +
      (truncDoc ? `<span class="ac-doc">${escHtml(truncDoc)}</span>` : "");
    el.addEventListener("mousedown", (e) => {
      e.preventDefault();
      acceptAutocomplete(i);
    });
    el.addEventListener("mouseenter", () => {
      if (it.fullDoc && it.fullDoc.length > 40) {
        showAcTooltip(it.fullDoc);
      } else {
        hideAcTooltip();
      }
    });
    el.addEventListener("mouseleave", () => {
      hideAcTooltip();
    });
    autocomplete.appendChild(el);
  });
  autocomplete.hidden = false;

  // Show tooltip for current active item if it has a long doc
  const active = acItems[acIndex];
  if (active?.fullDoc && active.fullDoc.length > 40) {
    showAcTooltip(active.fullDoc);
  } else {
    hideAcTooltip();
  }
}

function showAcTooltip(text) {
  if (!acTooltip) {
    acTooltip = document.createElement("div");
    acTooltip.className = "ac-tooltip";
    form.appendChild(acTooltip);
  }
  acTooltip.textContent = text;
  acTooltip.hidden = false;
}

function hideAcTooltip() {
  if (acTooltip) acTooltip.hidden = true;
}

function acceptAutocomplete(i = acIndex) {
  const pick = acItems[i];
  if (!pick) return;
  const tok = currentTriggerToken();
  if (!tok) return;
  // Splice replacement in at the token position (preserving trailing text).
  const before = input.value.slice(0, tok.startOffset);
  const after = input.value.slice(tok.endOffset);
  input.value = before + pick.insert + after;
  const newCursor = (before + pick.insert).length;
  input.setSelectionRange(newCursor, newCursor);
  autocomplete.hidden = true;
  hideAcTooltip();
  autosize();
  input.focus();
}

// ---------------------------------------------------------------------------
// Keyboard shortcuts overlay

function toggleShortcuts() {
  shortcutsOverlay.classList.toggle("visible");
}

function hideShortcuts() {
  shortcutsOverlay.classList.remove("visible");
}

function shortcutsVisible() {
  return shortcutsOverlay.classList.contains("visible");
}

shortcutsClose.addEventListener("click", hideShortcuts);

shortcutsOverlay.addEventListener("click", (e) => {
  if (e.target === shortcutsOverlay) hideShortcuts();
});

// ---------------------------------------------------------------------------
// Keyboard

input.addEventListener("keydown", (e) => {
  // Autocomplete nav first
  if (!autocomplete.hidden) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      acIndex = (acIndex + 1) % acItems.length;
      renderAutocomplete();
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      acIndex = (acIndex - 1 + acItems.length) % acItems.length;
      renderAutocomplete();
      return;
    }
    if (e.key === "Tab" || e.key === "Enter") {
      e.preventDefault();
      acceptAutocomplete();
      return;
    }
    if (e.key === "Escape") {
      autocomplete.hidden = true;
      hideAcTooltip();
      return;
    }
  }

  // ⌘/Ctrl+Enter submits
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    e.preventDefault();
    form.requestSubmit();
    return;
  }

  // ⌘/Ctrl+K = new session
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    clearBtn.click();
    return;
  }

  // ⌘/Ctrl+T = new tab
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "t") {
    e.preventDefault();
    createTab("new", true);
    return;
  }

  // ⌘/Ctrl+W = close tab
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "w") {
    e.preventDefault();
    closeTab(tabManager.activeIdx);
    return;
  }

  // History navigation with ↑/↓ when the cursor is at the start of the input
  if (e.key === "ArrowUp" && input.selectionStart === 0 && input.value === "") {
    if (userHistory.length === 0) return;
    e.preventDefault();
    if (historyCursor === -1) historyDraft = input.value;
    historyCursor = Math.max(0, historyCursor === -1 ? userHistory.length - 1 : historyCursor - 1);
    input.value = userHistory[historyCursor];
    autosize();
    input.setSelectionRange(input.value.length, input.value.length);
    return;
  }
  if (e.key === "ArrowDown" && historyCursor !== -1) {
    e.preventDefault();
    historyCursor += 1;
    if (historyCursor >= userHistory.length) {
      historyCursor = -1;
      input.value = historyDraft;
    } else {
      input.value = userHistory[historyCursor];
    }
    autosize();
    input.setSelectionRange(input.value.length, input.value.length);
    return;
  }
});

input.addEventListener("blur", () => {
  // Delay so mousedown on autocomplete can fire first.
  setTimeout(() => { autocomplete.hidden = true; hideAcTooltip(); }, 100);
});

// Global keyboard shortcuts
document.addEventListener("keydown", (e) => {
  // Esc: close overlays / stop generation
  if (e.key === "Escape") {
    if (!checkpointModal.classList.contains("hidden")) {
      e.preventDefault();
      hideCheckpointModal();
      return;
    }
    if (!fleetOverlay.classList.contains("hidden")) {
      e.preventDefault();
      hideFleetOverlay();
      return;
    }
    if (!mcpOverlay.classList.contains("hidden")) {
      e.preventDefault();
      hideMcpOverlay();
      return;
    }
    if (!diagOverlay.classList.contains("hidden")) {
      e.preventDefault();
      hideDiagOverlay();
      return;
    }
    if (!recentSessionsModal.classList.contains("hidden")) {
      e.preventDefault();
      hideRecentSessionsModal();
      return;
    }
    if (shortcutsVisible()) {
      e.preventDefault();
      hideShortcuts();
      return;
    }
    if (busy) {
      e.preventDefault();
      stopBtn.click();
      return;
    }
  }

  // ? key when textarea is not focused — toggle shortcuts
  if (e.key === "?" && document.activeElement !== input) {
    e.preventDefault();
    toggleShortcuts();
    return;
  }

  // "/" — focus composer and trigger slash autocomplete (when not already
  // typing into an input/textarea/select).
  if (e.key === "/" && !e.metaKey && !e.ctrlKey && !e.altKey) {
    const t = document.activeElement;
    const tag = t?.tagName;
    const editable = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || t?.isContentEditable;
    if (!editable) {
      e.preventDefault();
      input.focus();
      // Insert "/" at the start so the existing slash autocomplete fires.
      input.value = "/" + input.value;
      input.setSelectionRange(1, 1);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return;
    }
  }

  // ⌘/Ctrl+T and ⌘/Ctrl+W from anywhere
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "t") {
    e.preventDefault();
    createTab("new", true);
    return;
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "w") {
    e.preventDefault();
    closeTab(tabManager.activeIdx);
    return;
  }
});

// ---------------------------------------------------------------------------
// Submit

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const prompt = input.value.trim();
  if (!prompt && pendingImages.length === 0 && pendingTextFiles.length === 0) return;
  input.value = "";
  autosize();
  autocomplete.hidden = true;
  hideAcTooltip();

  // Build the display text including file references
  let displayText = prompt;
  if (pendingTextFiles.length > 0) {
    for (const f of pendingTextFiles) {
      displayText += `\n\n\`\`\`${f.name}\n${f.content}\n\`\`\``;
    }
  }
  if (pendingImages.length > 0) {
    displayText += pendingImages.map((i) => `\n[image: ${i.name}]`).join("");
  }

  if (prompt) {
    userHistory.push(prompt);
    if (userHistory.length > 100) userHistory = userHistory.slice(-100);
  }
  historyCursor = -1;
  saveSettings();

  const userBubble = addMessage("user", displayText);

  // Update tab label to first prompt
  if (tabManager.active && (tabManager.active.label === "new" || tabManager.active.label === "session")) {
    tabManager.active.label = (prompt || "file").slice(0, 20);
    renderTabs();
    saveTabs();
  }

  // `/foo` = slash command (routed to obscura.cli.handle_command).
  // `$skill` / `@cmd` prefixes are real REPL tokens the host expands into
  // context blocks before sending — they're sent as regular prompts.
  const isCommand = prompt.startsWith("/");
  const context = ctxToggle.checked && !isCommand ? await getPageContext() : {};

  // Attach dropped images and text files to context
  if (pendingImages.length > 0) {
    context.images = pendingImages.map((i) => i.dataUrl);
  }
  if (pendingTextFiles.length > 0) {
    context.attached_files = pendingTextFiles.map((f) => ({ name: f.name, content: f.content }));
  }

  // Snapshot composer state — the queue entry owns its images/files even if
  // the user drops new ones before this turn fires.
  pendingImages = [];
  pendingTextFiles = [];
  renderDropPreview();

  const id =
    crypto.randomUUID?.() ?? `m-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const desc = { id, prompt, isCommand, context, userBubble };

  if (busy) {
    // Queue for later; render the user bubble in a dimmed "queued" state
    // until its turn actually dispatches.
    userBubble.parentElement?.classList.add("queued");
    sendQueue.push(desc);
    renderStatus();
    return;
  }

  dispatchSend(desc);
});

// Send a previously-prepared descriptor immediately.  Called either
// directly from submit when the panel is idle, or from drainQueue() when
// a turn completes.
function dispatchSend(desc) {
  desc.userBubble?.parentElement?.classList.remove("queued");

  const bubble = addMessage("assistant", "");
  bubble.parentElement?.classList.add("cursor");
  pending.set(desc.id, { bubble, streamedText: "", thinkingText: "", thinkingEl: null });

  setBusy(true);
  setLiveStatus("thinking…");

  try {
    if (!port) connect();
    if (desc.isCommand) {
      port.postMessage({
        type: MsgType.COMMAND,
        id: desc.id,
        raw: desc.prompt,
        backend: backendSel.value,
        ...(authToken ? { auth_token: authToken } : {}),
      });
    } else {
      port.postMessage({
        type: MsgType.SEND,
        id: desc.id,
        prompt: desc.prompt,
        backend: backendSel.value,
        session_id: sessionId,
        workspace: workspaceSel.value || undefined,
        context: desc.context,
        ...(authToken ? { auth_token: authToken } : {}),
      });
    }
  } catch (err) {
    pending.delete(desc.id);
    setBusy(false);
    setLiveStatus("");
    bubble.parentElement?.classList.remove("cursor");
    bubble.parentElement.classList.remove("assistant");
    bubble.parentElement.classList.add("error");
    bubble.textContent = `Error: ${err.message}`;
    drainQueue();
  }
}

// Fire the next queued descriptor when the current turn completes.  Safe
// to call whenever `busy` flips to false; no-ops if the queue is empty.
function drainQueue() {
  if (busy || sendQueue.length === 0) return;
  const next = sendQueue.shift();
  renderStatus();
  dispatchSend(next);
}

clearBtn.addEventListener("click", () => {
  sessionId = null;
  log.innerHTML = "";
  addMessage("system", "new session.");
  chrome.storage.local.remove(TRANSCRIPT_KEY);
  saveTabs();
});

stopBtn.addEventListener("click", () => {
  // Drop anything queued behind the in-flight turn so a single Stop press
  // clears the whole backlog rather than letting queued turns fire next.
  if (sendQueue.length) {
    for (const q of sendQueue) {
      q.userBubble?.parentElement?.classList.remove("queued");
      q.userBubble?.parentElement?.classList.add("cancelled");
    }
    sendQueue = [];
    renderStatus();
  }
  if (!pending.size) return;
  for (const id of pending.keys()) {
    try { port?.postMessage({ type: MsgType.CANCEL, target_id: id }); } catch {}
  }
});

reloadHostBtn.addEventListener("click", () => {
  addMessage("system", "restarting native host…");
  setHostStatus("restarting…", "warn");
  try { port?.postMessage({ type: "shutdown" }); } catch {}
  setTimeout(() => {
    try { port?.disconnect(); } catch {}
    port = null;
    connect();
  }, 400);
});

// ---------------------------------------------------------------------------
// Diagnose button (statusbar)

sbDiag.addEventListener("click", () => {
  try {
    port?.postMessage({ type: MsgType.DIAG, id: crypto.randomUUID?.() ?? `diag-${Date.now()}` });
  } catch {}
});

// ---------------------------------------------------------------------------
// Diagnostics overlay

function showDiagOverlay(data) {
  diagContent.innerHTML = "";
  const skip = new Set(["type", "id"]);
  const table = document.createElement("table");

  const addRow = (key, val) => {
    const tr = document.createElement("tr");
    const k = document.createElement("td");
    k.textContent = key;
    const v = document.createElement("td");
    v.textContent = typeof val === "object" && val !== null ? JSON.stringify(val) : String(val ?? "—");
    tr.append(k, v);
    table.appendChild(tr);
  };

  for (const [key, val] of Object.entries(data)) {
    if (skip.has(key)) continue;
    addRow(key, val);
  }

  diagContent.appendChild(table);

  // "Clear tool permissions" action link
  const clearLink = document.createElement("button");
  clearLink.type = "button";
  clearLink.textContent = "clear tool permissions";
  clearLink.style.cssText = "margin-top:10px;font:inherit;font-size:10.5px;color:var(--fg-ghost);background:transparent;border:1px solid var(--line-strong);border-radius:3px;padding:3px 8px;cursor:pointer;";
  clearLink.addEventListener("click", async () => {
    await clearToolPerms();
    clearLink.textContent = "cleared ✓";
    setTimeout(() => { clearLink.textContent = "clear tool permissions"; }, 1500);
  });
  diagContent.appendChild(clearLink);

  diagOverlay.classList.remove("hidden");
}

function hideDiagOverlay() {
  diagOverlay.classList.add("hidden");
}

diagClose.addEventListener("click", hideDiagOverlay);
diagOverlay.addEventListener("click", (e) => {
  if (e.target === diagOverlay) hideDiagOverlay();
});

// ---------------------------------------------------------------------------
// Warning banner (Feature 4)

function showWarningBanner(message) {
  warningText.textContent = message;
  warningBanner.classList.remove("hidden");
}

warningBanner.querySelector(".warning-dismiss").addEventListener("click", () => {
  warningBanner.classList.add("hidden");
});

// ---------------------------------------------------------------------------
// Fleet overlay (Feature 3)

function showFleetOverlay() {
  fleetOverlay.classList.remove("hidden");
  fleetContent.innerHTML = '<div class="checkpoint-empty">loading…</div>';
  // Send /agent list command
  const id = crypto.randomUUID?.() ?? `fl-${Date.now()}`;
  fleetPendingId = id;
  const bubble = document.createElement("div");
  pending.set(id, { bubble, streamedText: "", silent: true });
  try {
    port?.postMessage({ type: MsgType.COMMAND, id, raw: "/agent list", backend: backendSel.value });
  } catch {
    // Fallback: request diag data
    port?.postMessage({ type: MsgType.DIAG, id: crypto.randomUUID?.() ?? `diag-${Date.now()}` });
  }
}

function hideFleetOverlay() {
  fleetOverlay.classList.add("hidden");
  fleetPendingId = null;
}

function renderFleetContent(diagData, agentListText) {
  fleetContent.innerHTML = "";

  // If we have diag data, render a card grid
  if (diagData) {
    const cards = document.createElement("div");
    cards.className = "fleet-cards";
    const add = (key, val) => {
      const card = document.createElement("div");
      card.className = "fleet-card";
      const k = document.createElement("div");
      k.className = "fleet-card-key";
      k.textContent = key;
      const v = document.createElement("div");
      v.className = "fleet-card-val";
      v.textContent = String(val ?? "—");
      card.append(k, v);
      cards.appendChild(card);
    };
    add("kairos", diagData.kairos?.active ? "on" : "off");
    add("backend", diagData.backend || "—");
    add("workspace", diagData.workspace || "(none)");
    add("tools", diagData.tool_count ?? "—");
    add("turns", diagData.turn_count ?? "—");
    add("session active", diagData.session_active ? "yes" : "no");
    fleetContent.appendChild(cards);
    return;
  }

  // Parse /agent list text
  if (agentListText) {
    const lines = agentListText.split("\n").map((l) => l.trim()).filter(Boolean);
    const agents = lines.filter((l) => !l.startsWith("No ") && !l.startsWith("Usage") && !l.startsWith("#"));
    if (agents.length === 0) {
      const empty = document.createElement("div");
      empty.className = "mcp-empty";
      empty.textContent = "no agents running";
      fleetContent.appendChild(empty);
      return;
    }
    for (const line of agents) {
      const row = document.createElement("div");
      row.className = "fleet-agent-row";
      const name = document.createElement("span");
      name.className = "fleet-agent-name";
      name.textContent = line;
      const stat = document.createElement("span");
      stat.className = "fleet-agent-status";
      stat.textContent = "active";
      row.append(name, stat);
      fleetContent.appendChild(row);
    }
  }
}

sbFleet.addEventListener("click", showFleetOverlay);
fleetClose.addEventListener("click", hideFleetOverlay);
fleetOverlay.addEventListener("click", (e) => {
  if (e.target === fleetOverlay) hideFleetOverlay();
});

// ---------------------------------------------------------------------------
// MCP overlay (Feature 6)

function showMcpOverlay() {
  mcpOverlay.classList.remove("hidden");
  mcpContent.innerHTML = '<div class="mcp-empty">loading…</div>';
  const id = crypto.randomUUID?.() ?? `mcp-${Date.now()}`;
  mcpPendingId = id;
  const bubble = document.createElement("div");
  pending.set(id, { bubble, streamedText: "", silent: true });
  try {
    port?.postMessage({ type: MsgType.COMMAND, id, raw: "/mcp list", backend: backendSel.value });
  } catch {}
}

function hideMcpOverlay() {
  mcpOverlay.classList.add("hidden");
  mcpPendingId = null;
}

function renderMcpContent(text) {
  mcpContent.innerHTML = "";
  if (!text || !text.trim()) {
    const empty = document.createElement("div");
    empty.className = "mcp-empty";
    empty.textContent = "no MCP servers connected";
    mcpContent.appendChild(empty);
    return;
  }
  // Each line may describe a server: parse "name [status] [tools: N]"
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  const servers = lines.filter((l) => !l.startsWith("Usage") && !l.startsWith("#") && !l.startsWith("No "));
  if (servers.length === 0) {
    const empty = document.createElement("div");
    empty.className = "mcp-empty";
    empty.textContent = "no MCP servers connected";
    mcpContent.appendChild(empty);
    return;
  }
  for (const line of servers) {
    const card = document.createElement("div");
    card.className = "mcp-server-card";
    const name = document.createElement("div");
    name.className = "mcp-server-name";
    // Try to extract name and status
    const parts = line.split(/\s{2,}|\t/);
    name.textContent = parts[0] || line;
    card.appendChild(name);
    if (parts.length > 1) {
      const detail = document.createElement("div");
      detail.className = "mcp-server-detail";
      detail.textContent = parts.slice(1).join("  ");
      card.appendChild(detail);
    }
    mcpContent.appendChild(card);
  }
}

sbMcp.addEventListener("click", showMcpOverlay);
mcpClose.addEventListener("click", hideMcpOverlay);
mcpOverlay.addEventListener("click", (e) => {
  if (e.target === mcpOverlay) hideMcpOverlay();
});

mcpDiscoverBtn.addEventListener("click", () => {
  mcpContent.innerHTML = '<div class="mcp-empty">discovering…</div>';
  const id = crypto.randomUUID?.() ?? `mcp-${Date.now()}`;
  mcpPendingId = id;
  const bubble = document.createElement("div");
  pending.set(id, { bubble, streamedText: "", silent: true });
  try {
    port?.postMessage({ type: MsgType.COMMAND, id, raw: "/mcp discover", backend: backendSel.value });
  } catch {}
});

// ---------------------------------------------------------------------------
// Voice input (Feature 5)

{
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    micBtn?.classList.add("hidden");
  } else {
    const recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = navigator.language;
    let recording = false;
    let interimSpan = null;

    function clearInterim() {
      if (interimSpan) { interimSpan.remove(); interimSpan = null; }
    }

    micBtn.addEventListener("click", () => {
      if (recording) {
        recognition.stop();
        recording = false;
        micBtn.classList.remove("recording");
        clearInterim();
      } else {
        try {
          recognition.start();
          recording = true;
          micBtn.classList.add("recording");
        } catch {}
      }
    });

    recognition.onresult = (e) => {
      let interim = "";
      let final = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) {
          final += e.results[i][0].transcript;
        } else {
          interim += e.results[i][0].transcript;
        }
      }
      if (final) {
        clearInterim();
        input.value = (input.value + " " + final).trim();
        autosize();
      } else if (interim) {
        // Show interim result as a ghost preview
        clearInterim();
        interimSpan = document.createElement("span");
        interimSpan.className = "voice-interim";
        interimSpan.textContent = interim;
        interimSpan.style.cssText = "color:var(--fg-ghost);font-style:italic;font-size:11px;margin-left:4px;";
        status.parentElement?.insertBefore(interimSpan, status.nextSibling);
      }
    };

    recognition.onerror = () => {
      recording = false;
      micBtn.classList.remove("recording");
      clearInterim();
    };

    recognition.onend = () => {
      recording = false;
      micBtn.classList.remove("recording");
      clearInterim();
    };
  }
}

// ---------------------------------------------------------------------------
// Export conversation

sbExport.addEventListener("click", async () => {
  const lines = [];
  for (const msg of log.querySelectorAll(".msg")) {
    const role = [...msg.classList].find((c) =>
      ["user", "assistant", "system", "error"].includes(c),
    );
    if (!role || role === "system") continue;
    const raw = msg.dataset.raw;
    const text = raw ?? msg.querySelector(".body")?.textContent ?? "";
    if (!text.trim()) continue;

    const toolCards = msg.querySelectorAll(".tool-input");
    let toolMd = "";
    for (const pre of toolCards) {
      if (pre.textContent.trim()) {
        toolMd += `\n\`\`\`json\n${pre.textContent.trim()}\n\`\`\`\n`;
      }
    }

    if (role === "user") {
      lines.push(`**You:** ${text.trim()}`);
    } else if (role === "assistant") {
      if (toolMd) lines.push(toolMd.trim());
      lines.push(text.trim());
    } else {
      lines.push(`> ${text.trim()}`);
    }
    lines.push("");
  }

  const md = lines.join("\n");
  const date = new Date().toISOString().slice(0, 10);
  const sid = sessionId ? sessionId.slice(0, 8) : "unknown";
  const filename = `obscura-session-${sid}-${date}.md`;
  const blob = new Blob([md], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1000);
});

// ---------------------------------------------------------------------------
// Auth gate

function showAuthGate() {
  authGate.classList.remove("hidden");
  authTokenInput.value = "";
  setTimeout(() => authTokenInput.focus(), 50);
}

function hideAuthGate() {
  authGate.classList.add("hidden");
}

function submitAuthToken() {
  const val = authTokenInput.value.trim();
  if (!val) return;
  authToken = val;
  hideAuthGate();
}

authSubmit.addEventListener("click", submitAuthToken);
authTokenInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); submitAuthToken(); }
  if (e.key === "Escape") { e.preventDefault(); hideAuthGate(); }
});
authGate.addEventListener("click", (e) => {
  if (e.target === authGate) hideAuthGate();
});

// ---------------------------------------------------------------------------
// Theme switching

function updateThemeBtn(theme) {
  if (!sbTheme) return;
  if (theme === "light") {
    sbTheme.textContent = "○";
    sbTheme.title = "Switch to dark theme";
  } else {
    sbTheme.textContent = "◐";
    sbTheme.title = "Switch to light theme";
  }
}

async function loadTheme() {
  try {
    const store = await StorageManager.get([THEME_KEY]);
    const theme = store[THEME_KEY] || "dark";
    document.documentElement.setAttribute("data-theme", theme);
    updateThemeBtn(theme);
  } catch {
    document.documentElement.setAttribute("data-theme", "dark");
    updateThemeBtn("dark");
  }
}

sbTheme?.addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme") || "dark";
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  chrome.storage.local.set({ [THEME_KEY]: next });
  updateThemeBtn(next);
});

// ---------------------------------------------------------------------------
// Storage migration
//
// When a stored schema changes, bump STORAGE_VERSION and add a migration
// step here. Never silently rewrite: a teammate pulling without a
// migration will see their panel state reset on next launch, so the
// commit that bumps the version MUST ship the migration.

async function migrateStorage() {
  try {
    const store = await chrome.storage.local.get(STORAGE_VERSION_KEY);
    const current = store[STORAGE_VERSION_KEY] ?? 0;
    if (current === STORAGE_VERSION) return;

    // Future: for (let v = current + 1; v <= STORAGE_VERSION; v++) { … }

    await chrome.storage.local.set({ [STORAGE_VERSION_KEY]: STORAGE_VERSION });
  } catch (err) {
    // Logged-only: we never block boot on storage problems.
    console.warn("[obscura] storage migration failed", err);
  }
}

// ---------------------------------------------------------------------------
// Profile id — stable UUID generated the first time the extension runs in
// this Chrome profile. Attached to every message so the host can log it;
// the host uses it only for diagnostics (multi-profile collisions).

let profileId = null;

async function ensureProfileId() {
  try {
    const store = await chrome.storage.local.get(PROFILE_ID_KEY);
    if (store[PROFILE_ID_KEY]) {
      profileId = store[PROFILE_ID_KEY];
      return;
    }
    profileId = crypto.randomUUID?.() ?? `p-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    await chrome.storage.local.set({ [PROFILE_ID_KEY]: profileId });
  } catch {
    // If storage fails we fall back to ephemeral — better than blocking.
    profileId = `p-${Date.now()}`;
  }
}

// Tag an outbound message with the current profile id. Thin wrapper
// around the pure helper in ./messaging.js.
const tagMessage = (msg) => withProfileId(msg, profileId);

// ---------------------------------------------------------------------------
// Boot

(async () => {
  await migrateStorage();
  await ensureProfileId();
  loadTheme();
  loadSettings();
  loadSessionPicker();
  // Per-tab state is loaded via tab_context message from background after connect().
  // We also try to load it eagerly if chromeTabId is already known (e.g. reopened panel).
  loadTabState();
  // Restore the multi-tab strip from chrome.storage.local. If no prior
  // state exists, the default single seed tab created at module load
  // remains in place and will pick up whatever loadSettings/loadTabState
  // dropped into the log.
  await loadTabs();
  connect();
})();
