// Shared helpers for Puppeteer E2E tests.
//
// These tests spawn a real Chrome, load the unpacked extension, and talk
// to the *actual* native messaging host. That means they need:
//
//   1. The native-messaging manifest installed in the system dir where
//      Chrome looks for it (`~/Library/Application Support/Google/Chrome/
//      NativeMessagingHosts/com.obscura.host.json` on macOS,
//      `~/.config/google-chrome/NativeMessagingHosts/` on Linux). Run
//      `make ext-install` once and you're set.
//   2. A Python with obscura importable. The launcher handles this
//      automatically when run via `make ext-install`.
//
// If the manifest isn't there we SKIP rather than fail — contributors who
// haven't done local setup shouldn't be blocked by an E2E they're not
// running.

import { copyFileSync, existsSync, mkdirSync, rmSync } from "node:fs";
import { homedir, platform } from "node:os";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

export const EXTENSION_DIR = resolve(__dirname, "../..");

/**
 * Return the path Chrome will look at for our native-messaging host
 * manifest on the current platform, or null if unsupported.
 */
export function nativeHostManifestPath() {
  const home = homedir();
  if (platform() === "darwin") {
    return join(
      home,
      "Library/Application Support/Google/Chrome/NativeMessagingHosts/com.obscura.host.json",
    );
  }
  if (platform() === "linux") {
    return join(
      home,
      ".config/google-chrome/NativeMessagingHosts/com.obscura.host.json",
    );
  }
  return null;
}

/**
 * Check preconditions for running the E2E suite. Returns an object with
 * ``{ok, reason}`` — pass ``reason`` into ``test.skip(ok ? undefined :
 * reason)`` in the caller.
 */
export function checkPreconditions() {
  const manifest = nativeHostManifestPath();
  if (!manifest) {
    return { ok: false, reason: `unsupported OS: ${platform()}` };
  }
  if (!existsSync(manifest)) {
    return {
      ok: false,
      reason: `native-messaging manifest missing at ${manifest}. Run 'make ext-install' first.`,
    };
  }
  return { ok: true, reason: "" };
}

/**
 * Read the pinned extension id from .keys/EXTENSION_ID. The extension's
 * pinned public key (in ``manifest.json`` → ``key``) means this is the
 * exact id Chrome will assign on load.
 */
export async function pinnedExtensionId() {
  const { readFile } = await import("node:fs/promises");
  const raw = await readFile(join(EXTENSION_DIR, ".keys/EXTENSION_ID"), "utf8");
  return raw.trim();
}

/**
 * Where Puppeteer's bundled "Chrome for Testing" looks for native-messaging
 * host manifests. Differs from the system Chrome path — so we mirror the
 * existing manifest across before launching the test browser.
 */
export function puppeteerChromeManifestDir() {
  const home = homedir();
  if (platform() === "darwin") {
    return join(
      home,
      "Library/Application Support/Google/Chrome for Testing/NativeMessagingHosts",
    );
  }
  if (platform() === "linux") {
    // Chrome for Testing reuses the standard google-chrome profile dir
    // on Linux, so no mirroring needed there.
    return null;
  }
  return null;
}

/**
 * Ensure Puppeteer's Chrome for Testing can see our native-messaging host.
 * Returns a cleanup function the caller should invoke in afterAll().
 */
export function ensurePuppeteerChromeManifest() {
  const dest = puppeteerChromeManifestDir();
  if (!dest) return () => {};
  const src = nativeHostManifestPath();
  if (!src || !existsSync(src)) return () => {};
  mkdirSync(dest, { recursive: true });
  const destFile = join(dest, "com.obscura.host.json");
  copyFileSync(src, destFile);
  return () => {
    try { rmSync(destFile, { force: true }); } catch {}
  };
}

/**
 * Launch headful Chrome with our extension loaded. Returns the Puppeteer
 * ``Browser`` — caller owns closing it.
 *
 * Chrome **cannot** load MV3 extensions in pure headless mode today, so we
 * use ``headless: "new"`` only when the caller opts in (Linux CI uses
 * xvfb-run to fake a display; local dev typically just runs headful).
 */
export async function launchWithExtension({ headless = false } = {}) {
  const puppeteer = await import("puppeteer");
  return puppeteer.default.launch({
    headless,
    args: [
      `--disable-extensions-except=${EXTENSION_DIR}`,
      `--load-extension=${EXTENSION_DIR}`,
      "--no-sandbox",
      // Speed up first-run UX overlays that block our navigation.
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-background-networking",
    ],
  });
}
