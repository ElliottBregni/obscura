// Vitest config for Puppeteer E2E tests. Node env (not jsdom) so we can
// spawn a real Chrome via puppeteer; tests self-skip when the native
// host manifest isn't installed.
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/e2e/**/*.spec.js"],
    globals: true,
    // Puppeteer launches are slow and flaky under parallelism — run serially.
    maxConcurrency: 1,
    // Global timeout per test; individual tests still override with the
    // second arg of `it()`.
    testTimeout: 60_000,
    hookTimeout: 60_000,
  },
});
