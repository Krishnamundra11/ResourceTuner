/** Phone-width rendering: no horizontal scroll, the sidebar collapses to an accessible toggle,
 *  and the primary content is reachable. Run against the "mobile" project (Pixel 7 viewport). */

import { expect, test } from "@playwright/test";

test.use({ viewport: { width: 393, height: 851 } });

test("Overview has no horizontal overflow at phone width", async ({ page }) => {
  await page.goto("/ui/#/");
  await expect(page.getByRole("main").getByRole("heading", { name: "Overview" })).toBeVisible();
  const { scrollWidth, clientWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(scrollWidth).toBeLessThanOrEqual(clientWidth + 1);   // +1 for subpixel rounding
});

test("Live Lab has no horizontal overflow at phone width", async ({ page }) => {
  await page.goto("/ui/#/live-lab");
  await expect(page.getByRole("main").getByRole("heading", { name: "Live Lab" })).toBeVisible();
  const { scrollWidth, clientWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(scrollWidth).toBeLessThanOrEqual(clientWidth + 1);
});

test("the Agent Pipeline graph (React Flow) renders within the viewport at phone width", async ({ page }) => {
  await page.goto("/ui/#/pipeline");
  await expect(page.getByRole("main").getByRole("heading", { name: "Agent Pipeline" })).toBeVisible();
  const { scrollWidth, clientWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(scrollWidth).toBeLessThanOrEqual(clientWidth + 1);
});

test("the sidebar opens as a drawer at phone width and closes after navigating", async ({ page }) => {
  await page.goto("/ui/#/");
  // Below `lg` the sidebar is off-canvas; the hamburger button must open it.
  const navLiveLab = page.getByRole("navigation").getByRole("link", { name: "Live Lab", exact: true });
  await expect(navLiveLab).not.toBeInViewport();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await navLiveLab.click();
  await expect(page.getByRole("main").getByRole("heading", { name: "Live Lab" })).toBeVisible();
  // Navigating closes the drawer again, so it never sits open over the new page's content.
  const navOverview = page.getByRole("navigation").getByRole("link", { name: "Overview", exact: true });
  await expect(navOverview).not.toBeInViewport();
});

test("the backdrop closes the drawer without navigating", async ({ page }) => {
  await page.goto("/ui/#/");
  const navLiveLab = page.getByRole("navigation").getByRole("link", { name: "Live Lab", exact: true });
  await page.getByRole("button", { name: "Open navigation" }).click();
  await expect(navLiveLab).toBeInViewport();
  // Click the backdrop, not the drawer itself.
  await page.mouse.click(370, 400);
  await expect(navLiveLab).not.toBeInViewport();
  await expect(page.getByRole("main").getByRole("heading", { name: "Overview" })).toBeVisible();
});
