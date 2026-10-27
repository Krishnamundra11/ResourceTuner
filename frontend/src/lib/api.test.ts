import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, eventStreamUrl } from "./api";

afterEach(() => vi.unstubAllGlobals());

function respond(status: number, body: unknown) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(
    typeof body === "string" ? body : JSON.stringify(body),
    { status, headers: { "Content-Type": "application/json" } },
  )));
}

describe("ApiError", () => {
  it("surfaces the backend's own message from a detail object", async () => {
    respond(409, { detail: { error: "run_in_progress", message: "run dashboard-1 is already running" } });
    await expect(api.startRun({ scenario: "build_vs_indexer" }))
      .rejects.toThrow("run dashboard-1 is already running");
  });

  it("flags a 409 as a conflict so the UI can explain it rather than just failing", async () => {
    respond(409, { detail: { error: "run_in_progress", message: "busy" } });
    const err = await api.startRun({ scenario: "x" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).isConflict).toBe(true);
    expect((err as ApiError).isWarmingUp).toBe(false);
  });

  it("flags the sampler's 503 as warming up, not as an error to retry forever", async () => {
    respond(503, { status: "warming_up", error: null });
    const err = await api.snapshot().catch((e: unknown) => e);
    expect((err as ApiError).isWarmingUp).toBe(true);
  });

  it("reads a FastAPI validation error into something a person can act on", async () => {
    respond(422, { detail: [{ loc: ["body", "cycles"], msg: "Input should be less than or equal to 10" }] });
    await expect(api.startRun({ scenario: "x", cycles: 99 }))
      .rejects.toThrow("cycles: Input should be less than or equal to 10");
  });

  it("reports an unreachable backend as status 0 rather than throwing a raw network error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    const err = await api.snapshot().catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(0);
    expect((err as ApiError).message).toMatch(/Cannot reach/);
  });
});

describe("request shapes", () => {
  it("posts a run as JSON with a content type", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ run_id: "r1" }), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    await api.startRun({ scenario: "build_vs_indexer", cycles: 1 });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/runs");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(JSON.parse(String(init.body))).toEqual({ scenario: "build_vs_indexer", cycles: 1 });
  });

  it("drops undefined query parameters instead of sending the string 'undefined'", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) =>
      new Response(JSON.stringify({ runs: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await api.runs({ limit: 10, status: undefined });
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/runs?limit=10");
  });

  it("escapes a run id into the path", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) =>
      new Response(JSON.stringify({}), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await api.run("weird/id");
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/runs/weird%2Fid");
  });
});

describe("eventStreamUrl", () => {
  it("streams everything when no run is named", () => {
    expect(eventStreamUrl()).toBe("/api/events/stream");
  });

  it("carries the resume cursor so a reconnect does not replay the whole store", () => {
    expect(eventStreamUrl({ runId: "r1", afterSeq: 42 }))
      .toBe("/api/events/stream?run_id=r1&after_seq=42");
  });

  it("omits a zero cursor, which means 'from the beginning'", () => {
    expect(eventStreamUrl({ runId: "r1", afterSeq: 0 })).toBe("/api/events/stream?run_id=r1");
  });
});
