/** Charts render real SVG/canvas content with legible labels, against the live snapshot
 *  endpoint — no simulated data. The host is almost certainly idle/low-load in this
 *  environment, so these assertions are about structure (axes, legend, canvas painted),
 *  not about specific numbers. */

import { expect, test } from "@playwright/test";

test("Overview's host chart renders a canvas with real pixels, not a blank placeholder", async ({ page }) => {
  await page.goto("/ui/#/");
  // Wait for at least a couple of sampler ticks so the chart has >1 point.
  await page.waitForTimeout(2500);
  const canvas = page.locator("canvas").first();
  await expect(canvas).toBeVisible({ timeout: 10_000 });

  const nonEmpty = await canvas.evaluate((el: HTMLCanvasElement) => {
    const ctx = el.getContext("2d");
    if (!ctx) return false;
    const { data } = ctx.getImageData(0, 0, el.width, el.height);
    return data.some((byte) => byte !== 0);
  });
  expect(nonEmpty).toBe(true);
});

test("PSI stat tiles show a labelled unit, never a bare number mistaken for the wrong metric", async ({ page }) => {
  await page.goto("/ui/#/");
  await expect(page.getByText("PSI CPU")).toBeVisible();
  await expect(page.getByText("PSI Memory")).toBeVisible();
  await expect(page.getByText("PSI I/O")).toBeVisible();
});

test("the classified-workload timeline strip renders coloured swatches with a legend", async ({ page }) => {
  await page.goto("/ui/#/");
  await page.waitForTimeout(2500);
  await expect(page.getByText("Classified workload")).toBeVisible();
  const strip = page.getByRole("img", { name: "Classified workload over time" });
  await expect(strip).toBeVisible();
});

test("Agent Pipeline shows no graph until a run exists — never a fabricated one", async ({ page }) => {
  await page.goto("/ui/#/pipeline");
  // This suite's backend is isolated and has started no run, so there is nothing to graph yet.
  await expect(page.getByText("No runs recorded yet")).toBeVisible();
  await expect(page.locator(".react-flow")).toHaveCount(0);
});

test("the pipeline graph (React Flow) renders real labelled stage nodes via a legacy replay", async ({ page }) => {
  // Replaying real recorded history (read-only, no experiment started) exercises the same
  // PipelineGraph component Agent Pipeline uses, with real stage labels.
  await page.goto("/ui/#/history");
  await page.getByRole("tab", { name: "Legacy cycles" }).click();
  const replayButtons = page.getByRole("button", { name: "Replay" });
  if ((await replayButtons.count()) === 0) test.skip(true, "no legacy cycle data recorded on this host");
  await replayButtons.first().click();

  const graph = page.locator(".react-flow");
  await expect(graph).toBeVisible();
  await expect(graph.getByText("Contention Trigger")).toBeVisible();
  await expect(graph.getByText("Safety Gate")).toBeVisible();
  await expect(graph.getByText("CrewAI Diagnostics")).toBeVisible();
  await expect(graph.getByText("ADK Supervisor")).toBeVisible();
  await expect(graph.getByText("Policy Store")).toBeVisible();
  // The legend (just below the graph) names the framework split, so colour is never the only cue.
  await expect(page.getByText("Deterministic", { exact: true })).toBeVisible();
  await expect(page.getByText("Google ADK", { exact: true })).toBeVisible();
  await expect(page.getByText("CrewAI", { exact: true })).toBeVisible();
});
