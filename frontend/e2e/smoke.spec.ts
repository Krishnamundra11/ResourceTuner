import { expect, test } from "@playwright/test";

test("the SPA loads and shows the app chrome", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/ui\/?$/);
  await expect(page.getByRole("main").getByRole("heading", { name: "Overview" })).toBeVisible();
});
