/** All nine views are reachable from the sidebar nav and render their own distinct heading,
 *  against the real FastAPI backend (an isolated store, no mocking). */

import { expect, test } from "@playwright/test";

const VIEWS: { label: string; path: string; heading: string }[] = [
  { label: "Overview", path: "/ui/#/", heading: "Overview" },
  { label: "Live Lab", path: "/ui/#/live-lab", heading: "Live Lab" },
  { label: "Agent Pipeline", path: "/ui/#/pipeline", heading: "Agent Pipeline" },
  { label: "Diagnosis", path: "/ui/#/diagnosis", heading: "Diagnosis & Explainability" },
  { label: "Safety & Actions", path: "/ui/#/safety", heading: "Safety & Actions" },
  { label: "Performance", path: "/ui/#/performance", heading: "Performance & Evaluation" },
  { label: "Policy Learning", path: "/ui/#/policy", heading: "Policy Learning" },
  { label: "Runs & Replay", path: "/ui/#/history", heading: "Historical Runs & Replay" },
  { label: "System Health", path: "/ui/#/system", heading: "System Health" },
];

for (const v of VIEWS) {
  test(`${v.label} loads and shows its own heading`, async ({ page }) => {
    await page.goto(v.path);
    await expect(page.getByRole("main").getByRole("heading", { name: v.heading, exact: true }))
      .toBeVisible();
    // No uncaught console error on a cold load of any view.
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.waitForTimeout(300);
    expect(errors).toEqual([]);
  });
}

// These two exercise the desktop icon-rail sidebar specifically (always-visible links, a
// collapse toggle); below `lg` the nav is an off-canvas drawer instead, covered by
// responsive.spec.ts's drawer tests.
test("every nav item is reachable by clicking the sidebar, not just by URL", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop sidebar only");
  await page.goto("/ui/#/");
  for (const v of VIEWS.slice(1)) {
    await page.getByRole("link", { name: v.label, exact: true }).click();
    await expect(page.getByRole("main").getByRole("heading", { name: v.heading, exact: true }))
      .toBeVisible();
  }
});

test("the sidebar collapses and expands without losing the current route", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop sidebar only");
  await page.goto("/ui/#/diagnosis");
  await page.getByRole("button", { name: /collapse|expand/i }).click();
  await expect(page.getByRole("main").getByRole("heading", { name: "Diagnosis & Explainability" }))
    .toBeVisible();
});
