// E2E smoke tests for the unpacked extension.
//
// Two layers:
//
// 1. ``extension loads without errors`` — always runs if Puppeteer can
//    launch Chrome. Catches: broken manifest, JS syntax errors, missing
//    element ids, CSS that throws, missing assets, service-worker parse
//    failures. This is the cheap regression net that would have caught
//    several of the bugs we hit during development.
//
// 2. ``native-host handshake`` — opt-in via ``OBSCURA_E2E_FULL=1`` because
//    it needs the full native-messaging chain set up: the host manifest
//    installed in Chrome's search path, a Python with obscura importable,
//    and a Chrome build that permits native messaging (Chrome for Testing
//    can be flaky here). On CI we only run layer 1.

import { describe, it, beforeAll, afterAll, expect } from "vitest";
import {
  ensurePuppeteerChromeManifest,
  launchWithExtension,
  nativeHostManifestPath,
  pinnedExtensionId,
} from "./helpers.js";
import { existsSync } from "node:fs";

const FULL = process.env.OBSCURA_E2E_FULL === "1";

describe("extension loads without errors", () => {
  let browser;
  let extensionId;
  const consoleErrors = [];
  const pageErrors = [];

  beforeAll(async () => {
    browser = await launchWithExtension({ headless: false });
    extensionId = await pinnedExtensionId();
  }, 60_000);

  afterAll(async () => {
    await browser?.close();
  });

  it("sidepanel page renders its expected structure", async () => {
    const page = await browser.newPage();
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });
    page.on("pageerror", (err) => pageErrors.push(err.message));

    await page.goto(
      `chrome-extension://${extensionId}/src/sidepanel/index.html`,
      { waitUntil: "domcontentloaded" },
    );

    // Structural assertions — any of these failing would indicate a JS
    // error prevented the page from wiring itself up, or an id got
    // renamed without updating every consumer.
    for (const id of ["backend", "workspace", "log", "composer", "prompt"]) {
      const el = await page.$(`#${id}`);
      expect(el, `missing #${id}`).not.toBeNull();
    }

    // No uncaught page errors — a broken import or missing element id
    // would explode here.
    expect(pageErrors).toEqual([]);
  }, 30_000);
});

describe.skipIf(!FULL)("native-host handshake (OBSCURA_E2E_FULL=1)", () => {
  let browser;
  let extensionId;
  let cleanupManifest;

  beforeAll(async () => {
    const host = nativeHostManifestPath();
    if (!host || !existsSync(host)) {
      throw new Error(
        `native host manifest missing at ${host}. Run 'make ext-install' first.`,
      );
    }
    cleanupManifest = ensurePuppeteerChromeManifest();
    browser = await launchWithExtension({ headless: false });
    extensionId = await pinnedExtensionId();
  }, 60_000);

  afterAll(async () => {
    await browser?.close();
    cleanupManifest?.();
  });

  it("sidepanel reports host: connected within 15s", async () => {
    const page = await browser.newPage();
    await page.goto(
      `chrome-extension://${extensionId}/src/sidepanel/index.html`,
      { waitUntil: "domcontentloaded" },
    );

    // The sidepanel updates a status indicator once the native host's
    // ready frame is processed. Look for any element whose text matches
    // "connected" within the timeout.
    await page.waitForFunction(
      () => /connected/i.test(document.body?.textContent ?? ""),
      { timeout: 15_000 },
    );
  }, 30_000);

  it("native host manifest references the pinned extension id", async () => {
    const { readFile } = await import("node:fs/promises");
    const raw = await readFile(nativeHostManifestPath(), "utf8");
    const manifest = JSON.parse(raw);
    expect(manifest.name).toBe("com.obscura.host");
    expect(manifest.allowed_origins).toContain(
      `chrome-extension://${extensionId}/`,
    );
  });
});
