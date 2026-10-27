/** Overview shows live telemetry, so the thing worth pinning down is what it does with a metric
 *  the host cannot provide: it must read as unavailable, never as a plausible-looking zero. */

import { screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Overview from "./Overview";
import { renderApp, stubFetch } from "@/test/utils";
import type { Snapshot } from "@/lib/types";

// ECharts draws to a canvas, which jsdom does not implement. The charts are covered by the
// palette tests; here they are replaced so the assertions are about the numbers around them.
vi.mock("@/components/charts", () => ({
  TimeSeriesChart: () => <div data-testid="time-series" />,
  ClassActivityBars: () => <div data-testid="class-activity" />,
  WorkloadStrip: () => <div data-testid="workload-strip" />,
  SERIES_1: () => "#3987e5",
  SERIES_2: () => "#d95926",
}));

function snapshot(over: Partial<Snapshot> = {}): Snapshot {
  return {
    timestamp: Date.now() / 1000,
    host: {
      cpu_percent: 72.5, cores: 6, iowait_percent: 3.1, load_1m: 4.2,
      memory_percent: 61.0, memory_available_mib: 3200, memory_total_mib: 8192,
      disk_read_mib_s: 1.5, disk_write_mib_s: 12.25, disk_util_percent: 88,
      delayacct_enabled: true, psi_cpu: 31.2, psi_memory: 0, psi_io: 12.4,
    },
    classification: {
      workload: "BUILD", confidence: 0.82, dominance: 0.9, persistence: 0.82,
      needs_llm_fallback: false, fallback_reasons: [], background_activity: ["CODE_INDEXING"],
      unattributed_activity_percent: 4.2, unattributed_processes: [],
      class_activity_percent: { BUILD: 310.5, CODE_INDEXING: 48.2 },
      class_io_wait_percent: { CODE_INDEXING: 39.1 },
      window: 10,
    },
    containers: [{
      name: "rt-builder", image: "build-sim", cpu_percent: 290.1, memory_mib: 410,
      memory_limit_mib: 1024, io_read_mib_s: 0.2, io_write_mib_s: 1.1,
      classified_as: "BUILD", ground_truth_label: "build",
    }],
    docker: { available: true, error: null },
    history: [],
    ...over,
  };
}

beforeEach(() => {
  vi.stubGlobal("EventSource", class { addEventListener() {} close() {} });
});
afterEach(() => vi.unstubAllGlobals());

describe("Overview", () => {
  it("shows the host's measured numbers", async () => {
    stubFetch({ "/api/snapshot": snapshot(), "/api/runs/active": { active: null } });
    renderApp(<Overview />);

    expect(await screen.findByText("72.5%")).toBeInTheDocument();    // CPU
    expect(screen.getByText(/6 vCPU · load 4.20/)).toBeInTheDocument();
    expect(screen.getByText("31.20%")).toBeInTheDocument();          // PSI cpu
  });

  it("renders a null PSI as unavailable and says why, rather than as 0", async () => {
    const s = snapshot();
    s.host.psi_cpu = null;
    s.host.psi_memory = null;
    s.host.psi_io = null;
    stubFetch({ "/api/snapshot": s, "/api/runs/active": { active: null } });
    renderApp(<Overview />);

    expect(await screen.findByText("PSI CPU")).toBeInTheDocument();
    expect(screen.getAllByText("/proc/pressure not readable on this host")).toHaveLength(3);
    // A real measured zero must still read as a number — so the dash cannot be a stand-in for it.
    expect(screen.queryByText("0.00%")).not.toBeInTheDocument();
  });

  it("keeps a measured zero visible as a number", async () => {
    stubFetch({ "/api/snapshot": snapshot(), "/api/runs/active": { active: null } });
    renderApp(<Overview />);
    // psi_memory is a real 0 in this snapshot: no swap on this host, so memory pressure shows
    // up as OOM kills rather than as PSI.
    expect(await screen.findByText("0.00%")).toBeInTheDocument();
  });

  it("renders a null disk utilisation as unavailable", async () => {
    const s = snapshot();
    s.host.disk_util_percent = null;
    stubFetch({ "/api/snapshot": s, "/api/runs/active": { active: null } });
    renderApp(<Overview />);
    expect(await screen.findByText("disk utilisation unavailable")).toBeInTheDocument();
  });

  it("reports an unreachable Docker rather than an empty container list", async () => {
    stubFetch({
      "/api/snapshot": snapshot({
        containers: [], docker: { available: false, error: "permission denied on /var/run/docker.sock" },
      }),
      "/api/runs/active": { active: null },
    });
    renderApp(<Overview />);

    expect(await screen.findByText("Docker is not reachable")).toBeInTheDocument();
    expect(screen.getByText(/permission denied/)).toBeInTheDocument();
    expect(screen.getByText("Docker unavailable")).toBeInTheDocument();
  });

  it("marks the ground-truth column as validation-only, since the classifier never reads it", async () => {
    stubFetch({ "/api/snapshot": snapshot(), "/api/runs/active": { active: null } });
    renderApp(<Overview />);

    const table = await screen.findByRole("table");
    expect(within(table).getByText("rt-builder")).toBeInTheDocument();
    expect(screen.getByText(/the classifier never reads it/)).toBeInTheDocument();
  });

  it("surfaces the sampler's own warm-up state instead of rendering zeroes", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ status: "warming_up", error: null }), { status: 503 })));
    renderApp(<Overview />);
    expect(await screen.findByText(/warming up/)).toBeInTheDocument();
  });

  it("names the fallback reasons when the classifier is not confident", async () => {
    const s = snapshot();
    s.classification!.needs_llm_fallback = true;
    s.classification!.fallback_reasons = ["confidence 0.31 below floor 0.60"];
    stubFetch({ "/api/snapshot": s, "/api/runs/active": { active: null } });
    renderApp(<Overview />);

    expect(await screen.findByText(/LLM fallback would be requested/)).toBeInTheDocument();
    expect(screen.getByText("confidence 0.31 below floor 0.60")).toBeInTheDocument();
  });
});

describe("Overview — a class with no confidence score", () => {
  it("does not render a null confidence as 0%", async () => {
    // The live classifier reports IDLE with confidence null: there is no winning class to be
    // confident about, which is not the same as being 0% confident in one.
    const s = snapshot();
    s.classification = {
      ...s.classification!,
      workload: "IDLE", confidence: null, dominance: null, persistence: null,
      class_activity_percent: {}, class_io_wait_percent: {},
    };
    stubFetch({ "/api/snapshot": s, "/api/runs/active": { active: null } });
    renderApp(<Overview />);

    expect(await screen.findByText("no confidence score")).toBeInTheDocument();
    expect(screen.queryByText("0% confidence")).not.toBeInTheDocument();
  });
});
