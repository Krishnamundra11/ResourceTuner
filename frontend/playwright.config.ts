/** Browser-level verification of the built control centre.
 *
 *  Runs against the real FastAPI backend serving the real built SPA — not a mock. The backend
 *  is started fresh for the run (see `webServer` below) with an isolated, empty event/action/
 *  policy store, so these tests never touch whatever data already exists in `data/`. No test in
 *  this suite starts a real scenario: Live Lab's Start flow is verified up to and including the
 *  confirmation dialog, never past it, so no container is ever launched by this suite. */

import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PORT = 8099;
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..");

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,       // all specs share one backend instance and its one-run-at-a-time rule
  workers: 1,                  // force true serial execution across files, not just within one
  retries: 0,
  reporter: [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile", use: { ...devices["Pixel 7"] } },
  ],
  webServer: {
    // launch_server.py injects an isolated EventStore/RunManager via create_app()'s own
    // injection point, so the real project's data/events.db is never touched by this suite.
    command: `${REPO_ROOT}/venv/bin/python ${path.join(__dirname, "e2e", "launch_server.py")} ` +
             `--port ${PORT} --data-dir ${path.join(__dirname, ".e2e-data")}`,
    url: `http://127.0.0.1:${PORT}/api/scenarios`,
    reuseExistingServer: false,
    timeout: 30_000,
    cwd: REPO_ROOT,
  },
});
