/** Historical Runs & Replay, driven in a real browser against the real backend's legacy data
 *  (data/vertical_slice_log.jsonl — not the isolated event store, since those endpoints are
 *  read-only by construction and reading real history is the point).
 *
 *  The thing this suite exists to prove: however the replay is driven — Play, Next, the
 *  scrubber, clicking a stage chip — the only network traffic it ever produces is GET. */

import { expect, test } from "@playwright/test";

test("declares itself read-only up front", async ({ page }) => {
  await page.goto("/ui/#/history");
  await expect(page.getByText("read-only", { exact: true })).toBeVisible();
  await expect(page.getByText(/cannot act even by mistake/)).toBeVisible();
});

test("replaying a legacy cycle sends only GET requests to the backend", async ({ page }) => {
  const nonGet: { method: string; url: string }[] = [];
  page.on("request", (req) => {
    if (req.url().includes("/api/") && req.method() !== "GET") {
      nonGet.push({ method: req.method(), url: req.url() });
    }
  });

  await page.goto("/ui/#/history");
  await page.getByRole("tab", { name: "Legacy cycles" }).click();
  const replayButtons = page.getByRole("button", { name: "Replay" });
  if ((await replayButtons.count()) === 0) test.skip(true, "no legacy cycle data recorded on this host");
  await replayButtons.first().click();

  // Drive it hard: play, let it auto-advance, pause, step both directions, scrub, click chips.
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await page.waitForTimeout(2500);
  const pauseBtn = page.getByRole("button", { name: "Pause", exact: true });
  if (await pauseBtn.isVisible().catch(() => false)) await pauseBtn.click();
  await page.getByRole("button", { name: "Next stage" }).click();
  await page.getByRole("button", { name: "Previous stage" }).click();
  await page.getByRole("button", { name: "Restart" }).click();
  const slider = page.getByRole("slider", { name: "Replay position" });
  await slider.click();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");

  expect(nonGet).toEqual([]);
});

test("a stage the legacy record never persisted reads as Evidence unavailable, not completed", async ({ page }) => {
  await page.goto("/ui/#/history");
  await page.getByRole("tab", { name: "Legacy cycles" }).click();
  const replayButtons = page.getByRole("button", { name: "Replay" });
  if ((await replayButtons.count()) === 0) test.skip(true, "no legacy cycle data recorded on this host");
  await replayButtons.first().click();

  // classify and diagnostics are documented as never persisted pre-event-layer (history.py).
  await page.getByRole("button", { name: "Workload Classifier" }).click();
  await expect(page.getByText(/Evidence unavailable\./)).toBeVisible();
  await expect(page.getByText(/It is not reported as completed, skipped or failed/)).toBeVisible();
});

test("switching between recorded runs and legacy cycles never POSTs", async ({ page }) => {
  const nonGet: string[] = [];
  page.on("request", (req) => {
    if (req.url().includes("/api/") && req.method() !== "GET") nonGet.push(req.method());
  });
  await page.goto("/ui/#/history");
  await page.getByRole("tab", { name: "Recorded runs" }).click();
  await expect(page.getByText("No runs recorded yet")).toBeVisible();
  await page.getByRole("tab", { name: "Legacy cycles" }).click();
  expect(nonGet).toEqual([]);
});
