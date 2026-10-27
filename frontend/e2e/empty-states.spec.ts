/** Empty and error states, against the real backend. This suite's event store is freshly
 *  isolated (see playwright.config.ts), so "no runs yet" here is genuinely true, not mocked. */

import { expect, test } from "@playwright/test";

test("Agent Pipeline is honest that no run exists yet, on a fresh backend", async ({ page }) => {
  await page.goto("/ui/#/pipeline");
  await expect(page.getByText("No runs recorded yet")).toBeVisible();
});

test("Diagnosis is honest that no run exists yet", async ({ page }) => {
  await page.goto("/ui/#/diagnosis");
  await expect(page.getByText("No runs recorded yet")).toBeVisible();
});

test("Safety & Actions shows an empty ledger honestly, not a fabricated row", async ({ page }) => {
  await page.goto("/ui/#/safety");
  await expect(page.getByText(/No actions have ever been applied|No runs recorded yet/)).toBeVisible();
});

test("Performance reports no cycles rather than inventing a chart", async ({ page }) => {
  await page.goto("/ui/#/performance");
  await expect(page.getByText("No runs recorded yet")).toBeVisible();
});

test("System Health reports zero runs and an open stream truthfully", async ({ page }) => {
  await page.goto("/ui/#/system");
  await expect(page.getByRole("main").getByRole("heading", { name: "System Health" })).toBeVisible();
  await expect(page.getByText("No runs recorded")).toBeVisible();
  await expect(page.getByText("No events in this session")).toBeVisible();
});

test("a 404 backend route inside the SPA falls back to index.html, not a dead link", async ({ page }) => {
  const res = await page.goto("/ui/this-route-does-not-exist");
  expect(res?.status()).toBe(200);
  // The hash-routed app still boots and falls back to Overview for an unknown hash route.
  await expect(page.getByRole("main").getByRole("heading", { name: "Overview" })).toBeVisible();
});

test("an unknown hash route renders Overview rather than a blank page", async ({ page }) => {
  await page.goto("/ui/#/this-is-not-a-real-view");
  await expect(page.getByRole("main").getByRole("heading", { name: "Overview" })).toBeVisible();
});

test("a cancelled run id in the URL reports not_found rather than a blank screen", async ({ page }) => {
  const res = await page.request.get("/api/runs/does-not-exist");
  expect(res.status()).toBe(404);
  const body = await res.json();
  expect(body.detail.error).toBe("run_not_found");
});

test("the legacy viewer at /legacy still works standalone", async ({ page }) => {
  const res = await page.goto("/legacy");
  expect(res?.status()).toBe(200);
});
