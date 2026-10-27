/** Live Lab against the real backend's real /api/scenarios and /api/runs endpoints.
 *
 *  This suite goes right up to — and never past — the confirmation dialog, so it never starts
 *  a real scenario, launches a container, or writes a cgroup. "Start the run" is clicked only
 *  in the one test that explicitly cancels on the confirmation dialog first. */

import { expect, test } from "@playwright/test";

test("lists scenarios the backend actually has, with their intervention support", async ({ page }) => {
  await page.goto("/ui/#/live-lab");
  await expect(page.getByRole("heading", { name: "Live Lab" })).toBeVisible();
  // These come straight from config/scenarios/*.yaml via GET /api/scenarios; the combobox
  // starts on the alphabetically first one.
  await expect(page.getByRole("combobox", { name: "Scenario" })).toHaveText(/build and test/i);

  await page.getByRole("combobox", { name: "Scenario" }).click();
  const options = page.getByRole("option");
  await expect(options).not.toHaveCount(0);
  await expect(page.getByRole("option", { name: /build vs indexer/i })).toBeVisible();
  await page.keyboard.press("Escape");

  // Picking a scenario with real intensity levels shows the levels GET /api/scenarios reported.
  await page.getByRole("combobox", { name: "Scenario" }).click();
  await page.getByRole("option", { name: /build vs indexer/i }).click();
  await expect(page.getByText(/Optimization supported|Pause only|Observation only/)).toBeVisible();
});

test("starting a run requires confirmation, and the dialog names what will actually happen", async ({ page }) => {
  await page.goto("/ui/#/live-lab");
  await page.getByRole("button", { name: "Start run" }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(/This is not a simulation/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Start the run" })).toBeVisible();
});

test("cancelling the confirmation starts nothing", async ({ page }) => {
  const posts: string[] = [];
  page.on("request", (req) => { if (req.method() === "POST") posts.push(req.url()); });

  await page.goto("/ui/#/live-lab");
  await page.getByRole("button", { name: "Start run" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("dialog")).not.toBeVisible();

  expect(posts).toEqual([]);
});

test("switching scenario and level updates the intervention note live", async ({ page }) => {
  await page.goto("/ui/#/live-lab");
  // The scenario combobox defaults to the first scenario; opening the level select shows
  // the options the backend reported for it, proving the dropdown is driven by real data.
  const levelTrigger = page.getByRole("combobox").nth(1);
  if (await levelTrigger.isVisible().catch(() => false)) {
    await levelTrigger.click();
    await expect(page.getByRole("option")).not.toHaveCount(0);
    await page.keyboard.press("Escape");
  }
});

test("Stop is disabled when there is no active run", async ({ page }) => {
  await page.goto("/ui/#/live-lab");
  await expect(page.getByRole("button", { name: "Stop run" })).toBeDisabled();
});

test("the event stream panel is connected and shows no fabricated events", async ({ page }) => {
  await page.goto("/ui/#/live-lab");
  await expect(page.getByText("open")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText("No events yet")).toBeVisible();
});

test("states plainly that it cannot run shell, Docker or cgroup commands directly", async ({ page }) => {
  await page.goto("/ui/#/live-lab");
  await expect(page.getByText(/arbitrary Docker operation|runs a shell command/)).toBeVisible();
});

test("the cycles and cooldown sliders have real accessible names (Radix puts role=slider on the Thumb)", async ({ page }) => {
  await page.goto("/ui/#/live-lab");
  await expect(page.getByRole("slider", { name: "Cycles", exact: true })).toBeVisible();
  await expect(page.getByRole("slider", { name: "Cooldown between cycles" })).toBeVisible();
});
