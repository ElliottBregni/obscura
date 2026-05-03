// Smoke tests for the browser-ops module. We can't drive real chrome.* APIs
// from jsdom, but we can stub them and verify that handleBrowserTool routes
// the dispatcher correctly and posts a well-formed response frame.
import { describe, it, expect, vi, beforeEach } from "vitest";

// chrome.* needs to be in place BEFORE importing the module under test —
// it registers debugger event listeners at top level.
beforeEach(() => {
  vi.stubGlobal("chrome", {
    tabs: {
      query: vi.fn(async () => [{ id: 7, title: "t", url: "u", active: true, pinned: false }]),
    },
    debugger: {
      onDetach: { addListener: vi.fn() },
      onEvent: { addListener: vi.fn() },
    },
  });
});

describe("handleBrowserTool", () => {
  it("posts an ok response for list_tabs", async () => {
    const { handleBrowserTool } = await import("../src/sidepanel/browser_ops.js");
    const sent = [];
    await handleBrowserTool(
      { id: "abc", op: "list_tabs", args: {} },
      (resp) => sent.push(resp),
    );
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      type: "browser-tool-response",
      id: "abc",
      ok: true,
    });
    expect(sent[0].result).toEqual([
      { id: 7, title: "t", url: "u", active: true, pinned: false },
    ]);
  });

  it("posts an error response for an unknown op", async () => {
    const { handleBrowserTool } = await import("../src/sidepanel/browser_ops.js");
    const sent = [];
    await handleBrowserTool(
      { id: "xyz", op: "no_such_op", args: {} },
      (resp) => sent.push(resp),
    );
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      type: "browser-tool-response",
      id: "xyz",
      ok: false,
    });
    expect(sent[0].error).toMatch(/unknown op/);
  });
});
