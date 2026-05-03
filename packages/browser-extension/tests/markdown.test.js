import { describe, it, expect } from "vitest";
import {
  escHtml,
  inlineTokens,
  renderInline,
  markdownToHtml,
  splitFencedBlocks,
} from "../src/sidepanel/markdown.js";

describe("escHtml", () => {
  it("escapes the five dangerous HTML characters", () => {
    expect(escHtml(`<script>alert("x" & 'y')</script>`)).toBe(
      "&lt;script&gt;alert(&quot;x&quot; &amp; &#39;y&#39;)&lt;/script&gt;",
    );
  });

  it("leaves plain text unchanged", () => {
    expect(escHtml("just some words")).toBe("just some words");
  });
});

describe("inlineTokens", () => {
  it("renders backtick code", () => {
    expect(inlineTokens("use `foo()`")).toBe(
      'use <code class="inline">foo()</code>',
    );
  });

  it("renders markdown links with noopener noreferrer", () => {
    expect(inlineTokens("[obscura](https://example.com)")).toBe(
      '<a href="https://example.com" target="_blank" rel="noopener noreferrer">obscura</a>',
    );
  });

  it("rejects javascript: links by only matching http/https", () => {
    // The rendered output should keep the literal text because the
    // link regex doesn't match non-http(s) protocols.
    const out = inlineTokens("[click](javascript:alert(1))");
    expect(out).not.toContain("<a");
    expect(out).toContain("[click]");
  });

  it("renders bold and italic", () => {
    expect(inlineTokens("**bold** and *italic*")).toBe(
      "<strong>bold</strong> and <em>italic</em>",
    );
  });

  it("escapes HTML before applying any markdown", () => {
    // XSS regression guard — raw tags in the source must be escaped.
    expect(inlineTokens("<b>x</b>")).not.toContain("<b>x");
    expect(inlineTokens("<b>x</b>")).toContain("&lt;b&gt;");
  });
});

describe("splitFencedBlocks", () => {
  it("splits a mix of prose and code", () => {
    const parts = splitFencedBlocks(
      "intro\n```py\nprint('hi')\n```\nafter",
    );
    expect(parts).toEqual([
      { kind: "text", raw: "intro\n" },
      { kind: "code", lang: "py", raw: "print('hi')\n" },
      { kind: "text", raw: "\nafter" },
    ]);
  });

  it("treats missing lang as empty string", () => {
    const parts = splitFencedBlocks("```\nx\n```");
    expect(parts[0]).toEqual({ kind: "code", lang: "", raw: "x\n" });
  });
});

describe("renderInline", () => {
  it("wraps hyphen bullets as <ul>", () => {
    const out = renderInline("- one\n- two");
    expect(out).toBe("<ul><li>one</li><li>two</li></ul>");
  });

  it("renders headings with the right level", () => {
    expect(renderInline("## hello")).toBe("<h2>hello</h2>");
  });
});

describe("markdownToHtml", () => {
  it("renders a code block with a lang label", () => {
    const out = markdownToHtml("```js\nlet x = 1;\n```");
    expect(out).toContain('<span class="code-lang">js</span>');
    expect(out).toContain("<code>let x = 1;\n</code>");
  });

  it("escapes HTML inside code blocks", () => {
    const out = markdownToHtml("```\n<img src=x onerror=1>\n```");
    expect(out).toContain("&lt;img src=x onerror=1&gt;");
    expect(out).not.toContain("<img ");
  });
});
