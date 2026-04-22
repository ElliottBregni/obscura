// Vitest configuration for the *unit* tests.
// jsdom so tests can touch DOM helpers without a real browser.
//
// E2E tests under tests/e2e/ use Puppeteer and need Node env — see
// vitest.e2e.config.js for that suite.
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.js"],
    exclude: ["tests/e2e/**", "node_modules/**"],
    globals: true,
  },
});
