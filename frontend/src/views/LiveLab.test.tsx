/** Live Lab is the only view with mutating controls, so these tests pin down exactly what it
 *  can do: post a run to the backend's own endpoint, and stop one. The backend here is a fetch
 *  stub — no run is ever started against a real pipeline by this suite. */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import LiveLab from "./LiveLab";
import { FakeEventSource, renderApp, stubFetch } from "@/test/utils";
import type { Run, Scenario } from "@/lib/types";

const scenario: Scenario = {
  name: "build_vs_indexer",
  description: "A build competing with a code indexer for CPU and disk.",
  levels: ["low", "medium", "high"],
  default_level: "medium",
  containers: { builder: "build-sim", indexer: "indexer-sim" },
  workloads: ["build", "code_indexing"],
  resource_dimensions: ["cpu", "io"],
  intervention: "supported",
  intervention_note: "a background target can be deprioritised, throttled or paused",
};

const observationOnly: Scenario = {
  ...scenario,
  name: "build_vs_ramhog",
  description: "A build competing with a memory hog.",
  intervention: "observation_only",
  intervention_note: "no memory tool exists (this VM has no swap), so RAM pressure is observed, not acted on",
};

function run(over: Partial<Run> = {}): Run {
  return {
    run_id: "dashboard-1", kind: "dashboard", scenario: "build_vs_indexer", level: "medium",
    cycles_requested: 1, cycles_done: 0, status: "running", params: {},
    created_at: 1_700_000_000, started_at: 1_700_000_001, ended_at: null, error: null,
    active: true, thread_alive: true, stop_requested: false,
    ...over,
  };
}

beforeEach(() => {
  FakeEventSource.reset();
  vi.stubGlobal("EventSource", FakeEventSource);
});
afterEach(() => vi.unstubAllGlobals());

describe("Live Lab", () => {
  it("lists only the scenarios the backend reports, with what a cycle could do to each", async () => {
    stubFetch({
      "/api/scenarios": { scenarios: [scenario, observationOnly], max_cycles: 10 },
      "/api/runs/active": { active: null },
    });
    renderApp(<LiveLab />);

    await screen.findByText(/A build competing with a code indexer/);
    expect(screen.getByText("Optimization supported")).toBeInTheDocument();
    expect(screen.getByText(/a background target can be deprioritised/)).toBeInTheDocument();
  });

  it("does not post anything just from opening the view or choosing options", async () => {
    const { calls } = stubFetch({
      "/api/scenarios": { scenarios: [scenario], max_cycles: 10 },
      "/api/runs/active": { active: null },
    });
    renderApp(<LiveLab />);
    await screen.findByText(/A build competing with a code indexer/);

    expect(calls.every((c) => c.method === "GET")).toBe(true);
  });

  it("requires confirmation before it will start a real run", async () => {
    const { calls } = stubFetch({
      "/api/scenarios": { scenarios: [scenario], max_cycles: 10 },
      "/api/runs/active": { active: null },
    });
    renderApp(<LiveLab />);
    await screen.findByText(/A build competing with a code indexer/);

    await userEvent.click(screen.getByRole("button", { name: /start run/i }));

    // The dialog is open; nothing has been posted yet.
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/This is not a simulation/)).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("cancelling the confirmation posts nothing", async () => {
    const { calls } = stubFetch({
      "/api/scenarios": { scenarios: [scenario], max_cycles: 10 },
      "/api/runs/active": { active: null },
    });
    renderApp(<LiveLab />);
    await screen.findByText(/A build competing with a code indexer/);

    await userEvent.click(screen.getByRole("button", { name: /start run/i }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: /cancel/i }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("posts the confirmed request to the backend's own run endpoint", async () => {
    const { calls } = stubFetch({
      "/api/scenarios": { scenarios: [scenario], max_cycles: 10 },
      "/api/runs/active": { active: null },
      "/api/runs": () => run(),
    });
    renderApp(<LiveLab />);
    await screen.findByText(/A build competing with a code indexer/);

    await userEvent.click(screen.getByRole("button", { name: /start run/i }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: /start the run/i }));

    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.url).toBe("/api/runs");
    // The scenario is named, never a command: the backend decides what that name means.
    expect(post.body).toMatchObject({ scenario: "build_vs_indexer", level: "medium", cycles: 1 });
    expect(JSON.stringify(post.body)).not.toMatch(/docker|cgroup|sh -c|sudo/i);
  });

  it("posts a stop to the active run and nothing else", async () => {
    const { calls } = stubFetch({
      "/api/scenarios": { scenarios: [scenario], max_cycles: 10 },
      "/api/runs/active": { active: run() },
      "/api/runs/dashboard-1/stop": {
        run_id: "dashboard-1", status: "running", stop_requested: true,
        detail: "the pipeline stops after the current stage, then rolls back and cleans up",
      },
    });
    renderApp(<LiveLab />);

    const stop = await screen.findByRole("button", { name: /stop run/i });
    await userEvent.click(stop);

    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toBe("/api/runs/dashboard-1/stop");
    expect(await screen.findByText(/rolls back and cleans up/)).toBeInTheDocument();
  });

  it("disables Start while a run is active, so two runs can never be requested", async () => {
    stubFetch({
      "/api/scenarios": { scenarios: [scenario], max_cycles: 10 },
      "/api/runs/active": { active: run() },
    });
    renderApp(<LiveLab />);

    const start = await screen.findByRole("button", { name: /start run/i });
    expect(start).toBeDisabled();
    expect(screen.getByText(/Only one run may be active at a time/)).toBeInTheDocument();
  });

  it("explains a 409 conflict rather than showing a bare failure", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if ((init?.method ?? "GET") === "POST") {
        return new Response(JSON.stringify({ detail: { error: "run_in_progress", message: "busy" } }),
                            { status: 409 });
      }
      if (url.startsWith("/api/scenarios")) {
        return new Response(JSON.stringify({ scenarios: [scenario], max_cycles: 10 }), { status: 200 });
      }
      return new Response(JSON.stringify({ active: null }), { status: 200 });
    }));
    renderApp(<LiveLab />);
    await screen.findByText(/A build competing with a code indexer/);

    await userEvent.click(screen.getByRole("button", { name: /start run/i }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: /start the run/i }));

    expect(await screen.findByText(/Another run is already active/)).toBeInTheDocument();
  });

  it("states plainly that it has no shell, Docker or cgroup access", async () => {
    stubFetch({
      "/api/scenarios": { scenarios: [scenario], max_cycles: 10 },
      "/api/runs/active": { active: null },
    });
    renderApp(<LiveLab />);
    expect(await screen.findByText(/runs a shell command/)).toBeInTheDocument();
  });

  it("shows no fabricated progress when a run has emitted no events", async () => {
    stubFetch({
      "/api/scenarios": { scenarios: [scenario], max_cycles: 10 },
      "/api/runs/active": { active: run() },
    });
    renderApp(<LiveLab />);
    expect(await screen.findByText("No events yet")).toBeInTheDocument();
  });
});
