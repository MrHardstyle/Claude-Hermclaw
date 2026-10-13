import { existsSync } from "node:fs";
import { defineConfig, devices } from "@playwright/test";

// Pre-installed browsers (do not download): PLAYWRIGHT_BROWSERS_PATH defaults to /opt/pw-browsers when present.
if (!process.env.PLAYWRIGHT_BROWSERS_PATH && existsSync("/opt/pw-browsers")) process.env.PLAYWRIGHT_BROWSERS_PATH = "/opt/pw-browsers";
process.env.PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD ??= "1";

const uiPort = Number(process.env.HERMCLAW_E2E_UI_PORT ?? 14173);
// optional explicit browser binary (when @playwright/test and the installed browser build differ)
const executablePath = process.env.HERMCLAW_E2E_CHROMIUM || undefined;

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: /.*\.spec\.ts$/,
  globalSetup: "./tests/e2e/global-setup.ts",
  // the tests share one seeded database and mutate job state (controls), so they run serially
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  outputDir: "test-results",
  use: {
    baseURL: `http://127.0.0.1:${String(uiPort)}`,
    locale: "de-DE",
    timezoneId: "Europe/Berlin",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      testIgnore: /responsive\.spec\.ts$/,
      use: { ...devices["Desktop Chrome"], launchOptions: executablePath ? { executablePath } : {} },
    },
    {
      name: "mobile",
      testMatch: /responsive\.spec\.ts$/,
      use: { ...devices["Pixel 7"], launchOptions: executablePath ? { executablePath } : {} },
    },
  ],
});
