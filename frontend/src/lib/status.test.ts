import { describe, expect, it } from "vitest";
import {
  ALL_PAIRS_SAFE_SLOTS, PIPELINE_ORDER, RUN_STATUS_META, STAGE_LABELS, STAGE_STATUS_META,
  WORKLOAD_COLORS, WORKLOAD_ORDER, actionStatusTone, executionTone, orderWorkloads, verdictTone,
  workloadColor,
} from "./status";

describe("the workload palette", () => {
  // The validated palette depends on the ORDER as much as on the values: the five-slot set only
  // passes on the adjacent pairlist, so a reordering silently invalidates the validator run
  // recorded in status.ts.
  it("keeps the fixed slot order the validator was run against", () => {
    expect(WORKLOAD_ORDER).toEqual(["BUILD", "TESTING", "CODE_INDEXING", "DEV_SERVER", "UNKNOWN"]);
  });

  it("caps all-pairs chart forms at the three validated slots", () => {
    expect(ALL_PAIRS_SAFE_SLOTS).toBe(3);
  });

  it("gives every identity class its own token", () => {
    const tokens = WORKLOAD_ORDER.map((k) => WORKLOAD_COLORS[k]);
    expect(new Set(tokens).size).toBe(WORKLOAD_ORDER.length);
    expect(tokens.every((t) => t.startsWith("var(--color-wl-"))).toBe(true);
  });

  it("keeps IDLE and BACKGROUND off the identity ramp", () => {
    // They are states, not identities, and must not compete with a real workload.
    expect(WORKLOAD_COLORS.IDLE).not.toMatch(/--color-wl-/);
    expect(WORKLOAD_COLORS.BACKGROUND).not.toMatch(/--color-wl-/);
  });

  it("falls back to a neutral token for a class it has never seen", () => {
    expect(workloadColor("SOMETHING_NEW")).toBe("var(--color-fg-subtle)");
    expect(workloadColor(null)).toBe("var(--color-fg-subtle)");
  });
});

describe("orderWorkloads", () => {
  it("lays classes out in slot order however they arrive", () => {
    expect(orderWorkloads(["CODE_INDEXING", "BUILD", "TESTING"]))
      .toEqual(["BUILD", "TESTING", "CODE_INDEXING"]);
  });

  it("puts unknown classes after the identity slots and IDLE last", () => {
    const out = orderWorkloads(["IDLE", "BUILD", "SOMETHING_NEW"]);
    expect(out[0]).toBe("BUILD");
    expect(out[out.length - 1]).toBe("IDLE");
  });
});

describe("stage status mapping", () => {
  it("gives evidence_unavailable its own distinct appearance", () => {
    // A stage whose evidence was never persisted must not look completed, skipped or failed.
    const unavailable = STAGE_STATUS_META.evidence_unavailable;
    expect(unavailable.label).toBe("Evidence unavailable");
    for (const other of ["completed", "skipped", "failed", "waiting", "rejected"] as const) {
      expect(STAGE_STATUS_META[other].label).not.toBe(unavailable.label);
      expect(STAGE_STATUS_META[other].color).not.toBe(unavailable.color);
    }
  });

  it("labels every stage in the pipeline order", () => {
    for (const stage of PIPELINE_ORDER) {
      expect(STAGE_LABELS[stage], `missing label for ${stage}`).toBeTruthy();
    }
  });

  it("puts the gate after the decision and the controller after the gate", () => {
    const i = (s: string) => PIPELINE_ORDER.indexOf(s as (typeof PIPELINE_ORDER)[number]);
    expect(i("decision")).toBeLessThan(i("safety_gate"));
    expect(i("safety_gate")).toBeLessThan(i("controller"));
    expect(i("controller")).toBeLessThan(i("post_measure"));
    expect(i("post_measure")).toBeLessThan(i("evaluation"));
  });

  it("covers every run status", () => {
    for (const s of ["requested", "running", "completed", "failed", "cancelled"] as const) {
      expect(RUN_STATUS_META[s].label).toBeTruthy();
    }
  });
});

describe("verdict and execution tones", () => {
  it("reads DEGRADED, TIMEOUT and ERROR as bad", () => {
    for (const v of ["DEGRADED", "TIMEOUT", "ERROR"]) expect(verdictTone(v)).toBe("bad");
  });

  it("reads UNCHANGED as a warning rather than a success", () => {
    expect(verdictTone("UNCHANGED")).toBe("warn");
    expect(verdictTone("IMPROVED")).toBe("ok");
  });

  it("treats a blocked action as a rejection, not a skip", () => {
    expect(executionTone("blocked")).toBe("bad");
    expect(executionTone("rejected")).toBe("bad");
    expect(executionTone("no_action")).toBe("idle");
  });

  it("distinguishes a rollback from a failed rollback", () => {
    expect(actionStatusTone("rolled_back")).toBe("warn");
    expect(actionStatusTone("rollback_failed")).toBe("bad");
  });
});
