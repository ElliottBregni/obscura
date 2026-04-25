// E2E smoke tests for the unpacked extension.
//
// ``native-host handshake`` is opt-in via ``OBSCURA_E2E_FULL=1`` because
// it needs the full native-messaging chain set up: the host manifest
// installed in Chrome's search path, a Python with obscura importable,
// and a Chrome build that permits native messaging (Chrome for Testing
// can be flaky here).

import { describe, it, beforeAll, afterAll, expect } from "vitest";
import {
  ensurePuppeteerChromeManifest,
  launchWithExtension,
  nativeHostManifestPath,
  pinnedExtensionId,
} from "./helpers.js";
import { existsSync } from "node:fs";

const FULL = process.env.OBSCURA_E2E_FULL === "1";

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
