// Side-panel UI. Talks to the service worker on a long-lived Port, which
// proxies to the native messaging host running obscura.

const $ = (sel) => document.querySelector(sel);
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
const sbDiag = $("#sb-diag");
const sbExport = $("#sb-export");
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

const SETTINGS_KEY = "obscura.settings.v1";
const TRANSCRIPT_KEY = "obscura.transcript.v1";
const SESSIONS_KEY = "obscura.sessions.v1";

let port = null;
let sessionId = null;            // per-panel conversation id (from host)
let pending = new Map();         // msgId -> { bubble, toolBox, toolMap, streamedText, thinkingText, thinkingEl }
let busy = false;
let authToken = null;            // auth token for OBSCURA_AUTH_ENABLED sessions
let commandIndex = [];           // from ready.commands ({name, doc, subcommands})
let skillIndex = [];             // from ready.skills       (string[])
let atCommandIndex = [];         // from ready.at_commands  (string[])
let userHistory = [];            // prompts the user has sent, for ↑/↓ nav
let historyCursor = -1;          // -1 = at the live input
let historyDraft = "";           // saved current input when stepping into history
let kairosState = "off";        // "on" | "off"
let pendingImages = [];          // { dataUrl, name }[] for drag-drop / paste
let pendingTextFiles = [];       // { name, content }[] for drag-drop

// ---------------------------------------------------------------------------
// Multi-session tabs

const MAX_TABS = 8;
let tabs = [];       // [{ id, label, sessionId, logHTML, pending, streamStates }]
let activeTabIdx = 0;

function newTabId() {
  return `tab-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
}

function createTab(label = "new", activate = true) {
  if (tabs.length >= MAX_TABS) return;
  const tab = {
    id: newTabId(),
    label: label.slice(0, 20) || "new",
    sessionId: null,
    logHTML: "",
    pending: new Map(),
    streamStates: {},
  };
  tabs.push(tab);
  if (activate) switchTab(tabs.length - 1);
  renderTabs();
}

function closeTab(idx) {
  if (tabs.length <= 1) return;
  tabs.splice(idx, 1);
  if (activeTabIdx >= tabs.length) activeTabIdx = tabs.length - 1;
  if (activeTabIdx < 0) activeTabIdx = 0;
  restoreTab(activeTabIdx);
  renderTabs();
}

function switchTab(idx) {
  if (idx === activeTabIdx && tabs.length > 0) return;
  // save current tab state
  if (tabs[activeTabIdx]) {
    tabs[activeTabIdx].logHTML = log.innerHTML;
    tabs[activeTabIdx].sessionId = sessionId;
    tabs[activeTabIdx].pending = pending;
  }
  activeTabIdx = idx;
  restoreTab(idx);
  renderTabs();
}

function restoreTab(idx) {
  const tab = tabs[idx];
  if (!tab) return;
  log.innerHTML = tab.logHTML;
  sessionId = tab.sessionId;
  pending = tab.pending || new Map();
  scrollToBottom();
}

function renderTabs() {
  tabStrip.innerHTML = "";
  if (tabs.length <= 1) return; // hide tab strip when only 1 tab
  tabs.forEach((tab, i) => {
    const el = document.createElement("div");
    el.className = "tab-item" + (i === activeTabIdx ? " active" : "");
    const label = document.createElement("span");
    label.className = "tab-label";
    label.textContent = tab.label;
    const close = document.createElement("span");
    close.className = "tab-close";
    close.textContent = "×";
    close.addEventListener("click", (e) => { e.stopPropagation(); closeTab(i); });
    el.append(label, close);
    el.addEventListener("click", () => switchTab(i));
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
tabs.push({
  id: newTabId(),
  label: "session",
  sessionId: null,
  logHTML: "",
  pending: new Map(),
  streamStates: {},
});

// ---------------------------------------------------------------------------
// Settings + transcript persistence

async function loadSettings() {
  try {
    const store = await chrome.storage.local.get([SETTINGS_KEY, TRANSCRIPT_KEY]);
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
  chrome.storage.local.set({
    [SETTINGS_KEY]: {
      backend: backendSel.value,
      includeContext: ctxToggle.checked,
      history: userHistory.slice(-100),
    },
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
// Session metadata persistence

async function saveSessionMeta(sid, firstPrompt) {
  if (!sid) return;
  try {
    const store = await chrome.storage.local.get(SESSIONS_KEY);
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
    await chrome.storage.local.set({ [SESSIONS_KEY]: sessions });
    renderSessionPicker(sessions);
  } catch {}
}

async function loadSessionPicker() {
  try {
    const store = await chrome.storage.local.get(SESSIONS_KEY);
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
    port?.postMessage({ type: "send", id: crypto.randomUUID?.() ?? `r-${Date.now()}`, prompt: `/resume ${sid}`, backend: backendSel.value });
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

function setBusy(v) {
  busy = v;
  sendBtn.disabled = v;
  stopBtn.hidden = !v;
  status.textContent = v ? "running…" : "";
  status.classList.toggle("busy", v);
}

// ---------------------------------------------------------------------------
// Minimal markdown → DOM renderer.
// Zero-dep; handles fenced code, inline code, bold, italic, links, lists.

function escHtml(s) {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );
}

function renderMarkdown(target, raw) {
  // Freeze the source so the transcript saver can grab original markdown.
  target.parentElement.dataset.raw = raw;

  // Tokenize on fenced code blocks first so we don't mangle their contents.
  const parts = [];
  const fenceRe = /```([a-zA-Z0-9_+-]*)\n([\s\S]*?)```/g;
  let lastIdx = 0;
  let m;
  while ((m = fenceRe.exec(raw)) !== null) {
    if (m.index > lastIdx) parts.push({ kind: "text", raw: raw.slice(lastIdx, m.index) });
    parts.push({ kind: "code", lang: m[1] || "", raw: m[2] });
    lastIdx = fenceRe.lastIndex;
  }
  if (lastIdx < raw.length) parts.push({ kind: "text", raw: raw.slice(lastIdx) });

  let html = "";
  for (const p of parts) {
    if (p.kind === "code") {
      const langLabel = p.lang ? `<span class="code-lang">${escHtml(p.lang)}</span>` : "";
      html += `<pre class="code">${langLabel}<code>${escHtml(p.raw)}</code></pre>`;
    } else {
      html += renderInline(p.raw);
    }
  }
  target.innerHTML = html;

  // Add copy buttons to all code blocks.
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

function renderInline(text) {
  const lines = text.split("\n");
  let out = "";
  let inList = false;

  const finishList = () => {
    if (inList) { out += "</ul>"; inList = false; }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const listMatch = /^\s*[-*]\s+(.*)$/.exec(line);
    const headMatch = /^(#{1,6})\s+(.*)$/.exec(line);

    if (listMatch) {
      if (!inList) { out += "<ul>"; inList = true; }
      out += `<li>${inlineTokens(listMatch[1])}</li>`;
      continue;
    }
    finishList();

    if (headMatch) {
      const level = Math.min(headMatch[1].length, 6);
      out += `<h${level}>${inlineTokens(headMatch[2])}</h${level}>`;
      continue;
    }

    if (line.trim() === "") {
      out += "<br>";
    } else {
      out += inlineTokens(line) + "<br>";
    }
  }
  finishList();
  return out;
}

function inlineTokens(s) {
  // Order matters: code first (inside-out escaping), then links, then emphasis.
  s = escHtml(s);
  // `inline code`
  s = s.replace(/`([^`\n]+)`/g, (_m, g1) => `<code class="inline">${g1}</code>`);
  // [text](url)
  s = s.replace(
    /\[([^\]]+)\]\((https?:[^\s)]+)\)/g,
    (_m, t, u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`,
  );
  // **bold**
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  // *italic* / _italic_
  s = s.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?]|$)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,!?]|$)/g, "$1<em>$2</em>");
  return s;
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
// Live status (inside sidepanel, not just the footer)

function setLiveStatus(text) {
  status.textContent = text;
  status.classList.toggle("busy", busy);
}

// ---------------------------------------------------------------------------
// Rich widget detail rendering

function renderRichDetail(detail, toolName) {
  const container = document.createElement("div");
  container.className = "w-detail-rich";

  // Shell / Python commands
  if (toolName === "run_shell" || toolName === "run_python3") {
    const cmd = detail.command || detail.expression || detail.code || "";
    if (cmd) {
      const pre = document.createElement("pre");
      pre.className = "code";
      const code = document.createElement("code");
      code.textContent = cmd;
      pre.appendChild(code);
      container.appendChild(pre);
      return container;
    }
  }

  // File writes — show content
  if (toolName === "write_text_file" || toolName === "edit_text_file") {
    const content = detail.content || detail.new_string || detail.text || "";
    if (content) {
      const label = document.createElement("div");
      label.style.cssText = "font-size:10.5px;color:var(--fg-ghost);margin-bottom:4px;";
      label.textContent = detail.file_path || detail.path || toolName;
      container.appendChild(label);
      const pre = document.createElement("pre");
      pre.className = "code";
      const code = document.createElement("code");
      code.textContent = content.length > 4000 ? content.slice(0, 4000) + "\n…[truncated]" : content;
      pre.appendChild(code);
      container.appendChild(pre);
      if (detail.old_string) {
        const diffLabel = document.createElement("div");
        diffLabel.style.cssText = "font-size:10.5px;color:var(--red);margin:6px 0 2px;";
        diffLabel.textContent = "replaces:";
        container.appendChild(diffLabel);
        const oldPre = document.createElement("pre");
        oldPre.className = "code";
        oldPre.style.borderLeftColor = "var(--red)";
        const oldCode = document.createElement("code");
        oldCode.textContent = detail.old_string.length > 2000 ? detail.old_string.slice(0, 2000) + "\n…[truncated]" : detail.old_string;
        oldPre.appendChild(oldCode);
        container.appendChild(oldPre);
      }
      return container;
    }
  }

  // Default: key-value table
  const table = document.createElement("table");
  for (const [k, v] of Object.entries(detail)) {
    const row = document.createElement("tr");
    const keyCell = document.createElement("td");
    keyCell.textContent = k;
    const valCell = document.createElement("td");
    valCell.textContent = typeof v === "object" ? JSON.stringify(v, null, 2) : String(v);
    row.append(keyCell, valCell);
    table.appendChild(row);
  }
  container.appendChild(table);
  return container;
}

// ---------------------------------------------------------------------------
// Widgets

function renderWidget(msg) {
  // Plan approval widget
  if (msg.kind === "plan_approval") {
    renderPlanApprovalWidget(msg);
    return;
  }

  const wrap = document.createElement("div");
  wrap.className = `msg widget widget-${msg.kind || "confirm"}`;
  const r = document.createElement("div");
  r.className = "role";
  r.textContent = "?";
  const body = document.createElement("div");
  body.className = "body";

  const q = document.createElement("div");
  q.className = "w-question";
  q.textContent = msg.question || "(no question)";
  body.appendChild(q);

  if (msg.detail) {
    // Rich detail for tool_confirm
    if (msg.kind === "tool_confirm" && typeof msg.detail === "object") {
      const toolName = msg.detail.tool_name || "";
      const toolInput = msg.detail.input || msg.detail;
      body.appendChild(renderRichDetail(toolInput, toolName));
    } else {
      const det = document.createElement("pre");
      det.className = "w-detail";
      try {
        det.textContent = JSON.stringify(msg.detail, null, 2);
      } catch {
        det.textContent = String(msg.detail);
      }
      body.appendChild(det);
    }
  }

  const actions = Array.isArray(msg.actions) && msg.actions.length > 0
    ? msg.actions
    : ["ok"];
  const actionsRow = document.createElement("div");
  actionsRow.className = "w-actions";

  for (const action of actions) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "w-btn";
    btn.dataset.action = action;
    if (action === msg.default) btn.classList.add("default");
    btn.textContent = action.replace(/_/g, " ");
    btn.addEventListener("click", () => resolveWidget(msg.id, action, wrap));
    actionsRow.appendChild(btn);
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
        resolveWidget(msg.id, "reply", wrap, textInput.value);
      }
    });
    body.appendChild(textInput);
  }

  body.appendChild(actionsRow);
  wrap.append(r, body);
  log.appendChild(wrap);
  scrollToBottom();

  (textInput || actionsRow.querySelector(".default") || actionsRow.querySelector(".w-btn"))?.focus();
}

function renderPlanApprovalWidget(msg) {
  const wrap = document.createElement("div");
  wrap.className = "msg widget widget-plan_approval";
  const r = document.createElement("div");
  r.className = "role";
  r.textContent = "?";
  const body = document.createElement("div");
  body.className = "body";

  const q = document.createElement("div");
  q.className = "w-question";
  q.textContent = msg.question || "Plan approval requested";
  body.appendChild(q);

  // Plan text block
  const planBlock = document.createElement("div");
  planBlock.className = "w-plan-block";
  planBlock.textContent = msg.plan_text || msg.detail?.plan_text || msg.text || "(no plan)";
  body.appendChild(planBlock);

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
  body.appendChild(modifyWrap);

  // Action buttons
  const actionsRow = document.createElement("div");
  actionsRow.className = "w-actions";

  const approveBtn = document.createElement("button");
  approveBtn.type = "button";
  approveBtn.className = "w-btn default";
  approveBtn.dataset.action = "approve";
  approveBtn.textContent = "approve";
  approveBtn.addEventListener("click", () => resolveWidget(msg.id, "approve", wrap));

  const rejectBtn = document.createElement("button");
  rejectBtn.type = "button";
  rejectBtn.className = "w-btn";
  rejectBtn.dataset.action = "reject";
  rejectBtn.textContent = "reject";
  rejectBtn.addEventListener("click", () => resolveWidget(msg.id, "reject", wrap));

  const modifyBtn = document.createElement("button");
  modifyBtn.type = "button";
  modifyBtn.className = "w-btn";
  modifyBtn.dataset.action = "modify";
  modifyBtn.textContent = "modify";
  modifyBtn.addEventListener("click", () => {
    modifyWrap.hidden = !modifyWrap.hidden;
    if (!modifyWrap.hidden) modifyInput.focus();
  });

  actionsRow.append(approveBtn, rejectBtn, modifyBtn);
  body.appendChild(actionsRow);
  wrap.append(r, body);
  log.appendChild(wrap);
  scrollToBottom();
  approveBtn.focus();
}

function resolveWidget(widgetId, action, bubbleEl, text = "") {
  try {
    port?.postMessage({
      type: "widget-response",
      widget_id: widgetId,
      action,
      text,
    });
  } catch {}
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
    port?.postMessage({ type: "kairos", action: newState });
  } catch {}
});

// ---------------------------------------------------------------------------
// Checkpoint / rewind

sbRewind.addEventListener("click", () => {
  // Send /checkpoint list as a command
  const id = crypto.randomUUID?.() ?? `cp-${Date.now()}`;
  const bubble = addMessage("assistant", "");
  bubble.parentElement?.classList.add("cursor");
  pending.set(id, { bubble, streamedText: "" });
  setBusy(true);
  setLiveStatus("checkpoint…");
  try {
    port?.postMessage({ type: "command", id, raw: "/checkpoint list", backend: backendSel.value });
  } catch {}
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
      port?.postMessage({ type: "diag", id: crypto.randomUUID?.() ?? `diag-${Date.now()}` });
    } catch {
      chrome.tabs.create({ url: chrome.runtime.getURL("src/onboarding/index.html") });
    }
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

function connect() {
  hideDisconnectBanner();
  port = chrome.runtime.connect({ name: "sidepanel" });

  port.onMessage.addListener((msg) => {
    if (!msg || typeof msg !== "object") return;
    switch (msg.type) {
      case "bridge-ready":
        status.textContent = "";
        break;
      case "ready":
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
      case "thinking": {
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
      case "kairos": {
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
      case "tool_start":
        toolStart(msg.id, msg.tool_use_id, msg.tool_name);
        break;
      case "tool_delta":
        toolDelta(msg.id, msg.tool_use_id, msg.delta || "");
        break;
      case "tool_end":
        toolEnd(msg.id, msg.tool_use_id);
        setLiveStatus("thinking…");
        break;
      case "tool_result":
        toolResult(msg.id, msg.tool_use_id, msg.text || "");
        break;
      case "chunk": {
        const st = pending.get(msg.id);
        if (!st) return;
        st.streamedText = (st.streamedText || "") + (msg.text ?? "");
        renderMarkdown(st.bubble, st.streamedText);
        st.bubble.parentElement?.classList.add("cursor");
        scrollToBottom();
        break;
      }
      case "done": {
        const st = pending.get(msg.id);
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
        if (msg.session_id) {
          sessionId = msg.session_id;
          // Update tab label if it's the first message
          if (tabs[activeTabIdx] && tabs[activeTabIdx].label === "session") {
            const firstUser = log.querySelector(".msg.user .body");
            if (firstUser) {
              tabs[activeTabIdx].label = (firstUser.textContent || "").slice(0, 20) || "session";
              renderTabs();
            }
          }
          // Save session metadata
          const firstUser = log.querySelector(".msg.user .body");
          saveSessionMeta(sessionId, firstUser?.textContent || "");
        }
        if (pending.size === 0) { setBusy(false); setLiveStatus(""); }
        break;
      }
      case "widget": {
        renderWidget(msg);
        break;
      }
      case "diag": {
        showDiagOverlay(msg);
        break;
      }
      case "auth_required": {
        showAuthGate();
        break;
      }
      case "browser-tool": {
        handleBrowserTool(msg);
        break;
      }
      case "error": {
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
        if (pending.size === 0) { setBusy(false); setLiveStatus(""); }
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
    port.postMessage({ type: "ping", id: "boot" });
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
    closeTab(activeTabIdx);
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
    if (!diagOverlay.classList.contains("hidden")) {
      e.preventDefault();
      hideDiagOverlay();
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

  // ⌘/Ctrl+T and ⌘/Ctrl+W from anywhere
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "t") {
    e.preventDefault();
    createTab("new", true);
    return;
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "w") {
    e.preventDefault();
    closeTab(activeTabIdx);
    return;
  }
});

// ---------------------------------------------------------------------------
// Submit

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (busy) return;
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

  addMessage("user", displayText);

  // Update tab label to first prompt
  if (tabs[activeTabIdx] && (tabs[activeTabIdx].label === "new" || tabs[activeTabIdx].label === "session")) {
    tabs[activeTabIdx].label = (prompt || "file").slice(0, 20);
    renderTabs();
  }

  const bubble = addMessage("assistant", "");
  bubble.parentElement?.classList.add("cursor");

  const id =
    crypto.randomUUID?.() ?? `m-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  pending.set(id, { bubble, streamedText: "", thinkingText: "", thinkingEl: null });
  setBusy(true);
  setLiveStatus("thinking…");

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
    // Inject text file contents as code blocks appended to the prompt
    let fileText = "";
    for (const f of pendingTextFiles) {
      fileText += `\n\n\`\`\`${f.name}\n${f.content}\n\`\`\``;
    }
    // We'll include it in the prompt itself
    context.attached_files = pendingTextFiles.map((f) => ({ name: f.name, content: f.content }));
  }

  // Clear pending files
  pendingImages = [];
  pendingTextFiles = [];
  renderDropPreview();

  try {
    if (!port) connect();
    if (isCommand) {
      port.postMessage({
        type: "command",
        id,
        raw: prompt,
        backend: backendSel.value,
        ...(authToken ? { auth_token: authToken } : {}),
      });
    } else {
      port.postMessage({
        type: "send",
        id,
        prompt,
        backend: backendSel.value,
        session_id: sessionId,
        workspace: workspaceSel.value || undefined,
        context,
        ...(authToken ? { auth_token: authToken } : {}),
      });
    }
  } catch (err) {
    pending.delete(id);
    setBusy(false);
    setLiveStatus("");
    bubble.parentElement?.classList.remove("cursor");
    bubble.parentElement.classList.remove("assistant");
    bubble.parentElement.classList.add("error");
    bubble.textContent = `Error: ${err.message}`;
  }
});

clearBtn.addEventListener("click", () => {
  sessionId = null;
  log.innerHTML = "";
  addMessage("system", "new session.");
  chrome.storage.local.remove(TRANSCRIPT_KEY);
});

stopBtn.addEventListener("click", () => {
  if (!pending.size) return;
  for (const id of pending.keys()) {
    try { port?.postMessage({ type: "cancel", target_id: id }); } catch {}
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
    port?.postMessage({ type: "diag", id: crypto.randomUUID?.() ?? `diag-${Date.now()}` });
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
// Boot

loadSettings();
loadSessionPicker();
connect();
