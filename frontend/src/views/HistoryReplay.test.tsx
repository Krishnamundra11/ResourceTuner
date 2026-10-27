/** The replay view's defining property is that it cannot act. These tests assert it two ways:
 *  by what it renders for a record with no evidence, and by what it never sends to the backend. */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import HistoryReplay from "./HistoryReplay";
import { FakeEventSource, renderApp, stubFetch } from "@/test/utils";
import type { LegacyCycleDetail, LegacyCycleList, RunList } from "@/lib/types";

const runs: RunList = { runs: [], total: 0, limit: 100, offset: 0 };

const cycleList: LegacyCycleList = {
  source: "data/vertical_slice_log.jsonl",
  read_only: true,
  note: "Cycle summaries written before structured events existed: per-stage evidence was not persisted.",
  total: 1, limit: 100, offset: 0,
  cycles: [{
    cycle: 3, timestamp: 1_700_000_000, duration_s: 142.5,
    trigger: { resource: "cpu", metric: "cpu_psi", value: 31.2, threshold: 15.6 },
    delegated_to: ["cpu"], candidate_count: 1,
    action: { tool: "set_container_cpu_weight", target_id: "rt:indexer", weight: 200 },
    execution_status: "applied", gate_reasons: null,
    verdict: "IMPROVED", improvement_pct: 18.4, rolled_back: false,
    before_s: 44.1, after_s: 35.9, llm_degraded: false, promote: false,
  }],
};

/** The shape history.py produces: only what the row proves, everything else unavailable. */
const detail: LegacyCycleDetail = {
  source: "data/vertical_slice_log.jsonl",
  read_only: true,
  cycle: 3,
  summary: cycleList.cycles[0],
  stages: [
    { stage: "trigger", status: "completed", summary: "cpu_psi 31.2 >= 15.6",
      payload: { trigger: { metric: "cpu_psi", value: 31.2 } }, ts: null, duration_ms: null,
      derived_from: "cycle_summary" },
    { stage: "classify", status: "evidence_unavailable",
      summary: "classifier output was not persisted for this cycle",
      payload: {}, ts: null, duration_ms: null, derived_from: "cycle_summary" },
    { stage: "diagnostics", status: "evidence_unavailable",
      summary: "per-agent diagnoses were not persisted; only the filtered candidate actions were",
      payload: {}, ts: null, duration_ms: null, derived_from: "cycle_summary" },
    { stage: "evaluation", status: "completed", summary: "IMPROVED (18.4%), kept",
      payload: { evaluation: { verdict: "IMPROVED" } }, ts: null, duration_ms: null,
      derived_from: "cycle_summary" },
  ],
  raw: {},
};

function stubHistory() {
  return stubFetch({
    "/api/runs": runs,
    "/api/history/cycles/3": detail,
    "/api/history/cycles": cycleList,
  });
}

beforeEach(() => {
  FakeEventSource.reset();
  vi.stubGlobal("EventSource", FakeEventSource);
});
afterEach(() => vi.unstubAllGlobals());

async function openLegacyTab() {
  await userEvent.click(await screen.findByRole("tab", { name: /legacy cycles/i }));
}

describe("Historical Runs & Replay", () => {
  it("declares itself read-only", async () => {
    stubHistory();
    renderApp(<HistoryReplay />);
    expect(await screen.findByText("read-only")).toBeInTheDocument();
    expect(screen.getByText(/cannot act even by mistake/)).toBeInTheDocument();
  });

  it("sends only GET requests, however much the record is explored", async () => {
    const { calls } = stubHistory();
    renderApp(<HistoryReplay />);
    await openLegacyTab();
    await screen.findByText(/Cycle summaries written before structured events existed/);

    await userEvent.click(await screen.findByRole("button", { name: /^replay$/i }));
    await userEvent.click(await screen.findByRole("button", { name: /^play$/i }));
    await userEvent.click(await screen.findByRole("button", { name: /next stage/i }));

    // No POST, PUT, PATCH or DELETE can originate here — there is no endpoint for it to hit.
    expect(calls.map((c) => c.method)).toEqual(calls.map(() => "GET"));
  });

  it("says 'Evidence unavailable' for a stage the record never persisted", async () => {
    stubHistory();
    renderApp(<HistoryReplay />);
    await openLegacyTab();
    await screen.findByText(/Cycle summaries written before structured events existed/);
    await userEvent.click(await screen.findByRole("button", { name: /^replay$/i }));

    // 2 of the 4 stages in this record have no evidence.
    expect(await screen.findByText(/2 of 4 stages in this cycle have no persisted evidence/))
      .toBeInTheDocument();
  });

  it("does not report an unavailable stage as completed, skipped or failed", async () => {
    stubHistory();
    renderApp(<HistoryReplay />);
    await openLegacyTab();
    await userEvent.click(await screen.findByRole("button", { name: /^replay$/i }));

    // Step to the classify stage, whose evidence was never written.
    await userEvent.click(await screen.findByRole("button", { name: /Workload Classifier/i }));

    // The summary appears both on the graph node and in the detail panel, so assert on the
    // wording only the detail panel carries.
    expect(await screen.findByText(/Evidence unavailable\./)).toBeInTheDocument();
    expect(await screen.findByText(/It is not reported as completed, skipped or failed/))
      .toBeInTheDocument();
    expect((await screen.findAllByText(/classifier output was not persisted/)).length)
      .toBeGreaterThan(0);
  });

  it("shows a stage that was recorded, with its payload", async () => {
    stubHistory();
    renderApp(<HistoryReplay />);
    await openLegacyTab();
    await userEvent.click(await screen.findByRole("button", { name: /^replay$/i }));

    await userEvent.click(await screen.findByRole("button", { name: /Contention Trigger/i }));
    // Shown on the graph node and again in the detail panel below it.
    expect((await screen.findAllByText("cpu_psi 31.2 >= 15.6")).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/Stage payload/)).toBeInTheDocument();
    expect(screen.getByText(/"value": 31.2/)).toBeInTheDocument();
  });

  it("steps backwards and forwards through the record without refetching it", async () => {
    const { calls } = stubHistory();
    renderApp(<HistoryReplay />);
    await openLegacyTab();
    await userEvent.click(await screen.findByRole("button", { name: /^replay$/i }));

    const detailCallsBefore = calls.filter((c) => c.url.includes("/history/cycles/3")).length;
    await userEvent.click(screen.getByRole("button", { name: /next stage/i }));
    await userEvent.click(screen.getByRole("button", { name: /previous stage/i }));

    // A historical record never changes, so stepping must not hit the backend again.
    expect(calls.filter((c) => c.url.includes("/history/cycles/3")).length).toBe(detailCallsBefore);
  });

  it("reports an empty legacy log honestly instead of inventing a cycle", async () => {
    stubFetch({
      "/api/runs": runs,
      "/api/history/cycles": { ...cycleList, total: 0, cycles: [] },
    });
    renderApp(<HistoryReplay />);
    await openLegacyTab();
    expect(await screen.findByText("No legacy cycle records")).toBeInTheDocument();
  });

  it("reports an empty event store honestly on the recorded-runs tab", async () => {
    stubHistory();
    renderApp(<HistoryReplay />);
    expect(await screen.findByText("No runs recorded yet")).toBeInTheDocument();
  });

  it("renders the legacy summary row's own numbers, not derived ones", async () => {
    stubHistory();
    renderApp(<HistoryReplay />);
    await openLegacyTab();

    const table = await screen.findByRole("table");
    expect(within(table).getByText("IMPROVED")).toBeInTheDocument();
    expect(within(table).getByText("+18.40%")).toBeInTheDocument();
    expect(within(table).getByText("44.10s")).toBeInTheDocument();
    expect(within(table).getByText("35.90s")).toBeInTheDocument();
  });

  it("pauses when the scrubber is dragged, so Play cannot fight the user", async () => {
    stubHistory();
    renderApp(<HistoryReplay />);
    await openLegacyTab();
    await userEvent.click(await screen.findByRole("button", { name: /^replay$/i }));

    await userEvent.click(await screen.findByRole("button", { name: /^play$/i }));
    expect(await screen.findByRole("button", { name: /^pause$/i })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Contention Trigger/i }));
    await waitFor(() => expect(screen.getByRole("button", { name: /^play$/i })).toBeInTheDocument());
  });
});
