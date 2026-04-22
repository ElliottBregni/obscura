// Pure markdown → HTML helpers used by the panel. Extracted so Vitest can
// test them without loading all of sidepanel.js (which depends on chrome.*).
//
// Supports: fenced code blocks, headings, unordered lists, inline code,
// links, **bold**, *italic* / _italic_.
//
// Deliberately minimalist — we do not want a markdown-it dependency. If a
// feature needs GFM (tables, strikethrough), add it here with a test.

export function escHtml(s) {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );
}

export function inlineTokens(s) {
  // Order matters: code first (inside-out escaping), then links, then
  // emphasis. Changes to this order will break existing tests.
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

export function renderInline(text) {
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

/**
 * Parse ``raw`` into an array of text / code segments using fenced
 * ```lang\n…``` blocks as boundaries. Pure function, no DOM needed.
 */
export function splitFencedBlocks(raw) {
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
  return parts;
}

/**
 * Render markdown to an HTML string (no DOM side-effects). The panel's
 * full ``renderMarkdown`` additionally attaches copy buttons to code
 * blocks — that's separate because it needs a live DOM target.
 */
export function markdownToHtml(raw) {
  let html = "";
  for (const p of splitFencedBlocks(raw)) {
    if (p.kind === "code") {
      const langLabel = p.lang ? `<span class="code-lang">${escHtml(p.lang)}</span>` : "";
      html += `<pre class="code">${langLabel}<code>${escHtml(p.raw)}</code></pre>`;
    } else {
      html += renderInline(p.raw);
    }
  }
  return html;
}
