import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FakeEventSource } from "@/test/utils";
import { latestStageStatus, useEventStream } from "./useEventStream";
import type { StageEvent } from "@/lib/types";

function event(seq: number, over: Partial<StageEvent> = {}): StageEvent {
  return {
    seq, run_id: "run-1", cycle: 1, stage: "trigger", status: "completed",
    ts: 1_700_000_000 + seq, duration_ms: 10, summary: `event ${seq}`, payload: {},
    ...over,
  };
}

beforeEach(() => {
  FakeEventSource.reset();
  vi.stubGlobal("EventSource", FakeEventSource);
});
afterEach(() => vi.unstubAllGlobals());

describe("useEventStream", () => {
  it("opens one stream for the named run and reports it open", async () => {
    const { result } = renderHook(() => useEventStream({ runId: "run-1" }));
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.last.url).toContain("run_id=run-1");

    act(() => FakeEventSource.last.onopen?.());
    await waitFor(() => expect(result.current.state).toBe("open"));
  });

  it("keeps events in seq order even when they arrive out of order", async () => {
    const { result } = renderHook(() => useEventStream({ runId: "run-1" }));
    act(() => {
      FakeEventSource.last.push(event(3));
      FakeEventSource.last.push(event(1));
      FakeEventSource.last.push(event(2));
    });
    await waitFor(() => expect(result.current.events).toHaveLength(3));
    expect(result.current.events.map((e) => e.seq)).toEqual([1, 2, 3]);
  });

  it("drops a duplicate seq, which a resumed stream may legitimately repeat", async () => {
    const { result } = renderHook(() => useEventStream({ runId: "run-1" }));
    act(() => {
      FakeEventSource.last.push(event(7, { summary: "first delivery" }));
      FakeEventSource.last.push(event(7, { summary: "redelivered after reconnect" }));
    });
    await waitFor(() => expect(result.current.events).toHaveLength(1));
    expect(result.current.events[0].summary).toBe("first delivery");
  });

  it("advances the cursor to the newest seq seen, so a reconnect resumes from it", async () => {
    const { result } = renderHook(() => useEventStream({ runId: "run-1" }));
    act(() => {
      FakeEventSource.last.push(event(5));
      FakeEventSource.last.push(event(9));
      FakeEventSource.last.push(event(6));           // late arrival must not move it backwards
    });
    await waitFor(() => expect(result.current.events).toHaveLength(3));
    expect(result.current.lastSeq).toBe(9);
  });

  it("treats a dropped connection as reconnecting, because EventSource retries by itself", async () => {
    const { result } = renderHook(() => useEventStream({ runId: "run-1" }));
    act(() => FakeEventSource.last.onopen?.());
    await waitFor(() => expect(result.current.state).toBe("open"));

    act(() => FakeEventSource.last.onerror?.());
    await waitFor(() => expect(result.current.state).toBe("reconnecting"));
    expect(result.current.lastError).toMatch(/reconnect/i);
    expect(FakeEventSource.last.closed).toBe(false);   // the browser owns the retry, not us
  });

  it("ignores a malformed payload instead of throwing", async () => {
    const { result } = renderHook(() => useEventStream({ runId: "run-1" }));
    act(() => {
      const es = FakeEventSource.last;
      // Not JSON — the real stream could deliver a truncated frame.
      es.addEventListener("stage", () => {});
      const ev = new MessageEvent<string>("stage", { data: "{not json" });
      es.push(event(1));
      // deliver the broken frame through the same listener path
      for (const fn of (es as unknown as { listeners: Map<string, ((e: MessageEvent<string>) => void)[]> })
        .listeners.get("stage") ?? []) fn(ev);
    });
    await waitFor(() => expect(result.current.events).toHaveLength(1));
  });

  it("opens no stream at all when disabled", () => {
    const { result } = renderHook(() => useEventStream({ runId: "run-1", enabled: false }));
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(result.current.state).toBe("closed");
  });

  it("closes the stream when the component goes away", () => {
    const { unmount } = renderHook(() => useEventStream({ runId: "run-1" }));
    const es = FakeEventSource.last;
    unmount();
    expect(es.closed).toBe(true);
  });

  it("keeps at most maxEvents, dropping the oldest", async () => {
    const { result } = renderHook(() => useEventStream({ runId: "run-1", maxEvents: 3 }));
    act(() => {
      for (let i = 1; i <= 5; i++) FakeEventSource.last.push(event(i));
    });
    await waitFor(() => expect(result.current.events).toHaveLength(3));
    expect(result.current.events.map((e) => e.seq)).toEqual([3, 4, 5]);
  });
});

describe("latestStageStatus", () => {
  it("keeps the newest event per stage, so a running stage is replaced by its result", () => {
    const map = latestStageStatus([
      event(1, { stage: "safety_gate", status: "running" }),
      event(2, { stage: "safety_gate", status: "rejected" }),
    ], 1);
    expect(map.get("safety_gate")?.status).toBe("rejected");
  });

  it("leaves a stage that only ever reported running as running — nothing is inferred", () => {
    const map = latestStageStatus([event(1, { stage: "diagnostics", status: "running" })], 1);
    expect(map.get("diagnostics")?.status).toBe("running");
  });

  it("filters to the requested cycle", () => {
    const map = latestStageStatus([
      event(1, { cycle: 1, stage: "trigger", summary: "cycle one" }),
      event(2, { cycle: 2, stage: "trigger", summary: "cycle two" }),
    ], 1);
    expect(map.get("trigger")?.summary).toBe("cycle one");
  });

  it("keeps run-level events, which carry no cycle", () => {
    const map = latestStageStatus([event(1, { cycle: null, stage: "warmup" })], 1);
    expect(map.has("warmup")).toBe(true);
  });
});
