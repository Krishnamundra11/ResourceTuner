import { describe, expect, it } from "vitest";
import { DASH, duration, mib, ms, num, pct, seconds, signedPct, titleCase } from "./format";

describe("formatting a value the backend could not measure", () => {
  // The whole point of these helpers: an absent metric must never read as a real zero.
  it.each([null, undefined, NaN])("renders %s as the dash, not 0", (v) => {
    expect(num(v as number | null)).toBe(DASH);
    expect(pct(v as number | null)).toBe(DASH);
    expect(seconds(v as number | null)).toBe(DASH);
    expect(signedPct(v as number | null)).toBe(DASH);
  });

  it("keeps a real zero visible as a number", () => {
    expect(num(0)).toBe("0.0");
    expect(pct(0)).toBe("0.0%");
    expect(seconds(0)).toBe("0.00s");
  });

  it("renders a null memory or duration as the dash", () => {
    expect(mib(null)).toBe(DASH);
    expect(ms(null)).toBe(DASH);
    expect(duration(null, null)).toBe(DASH);
  });
});

describe("signedPct", () => {
  it("marks an improvement with an explicit plus", () => {
    expect(signedPct(12.345)).toBe("+12.35%");
  });

  it("leaves a degradation with its own minus sign", () => {
    expect(signedPct(-4.2)).toBe("-4.20%");
  });

  it("does not sign an exact zero", () => {
    expect(signedPct(0)).toBe("0.00%");
  });
});

describe("mib", () => {
  it("switches to GiB past 1024 MiB", () => {
    expect(mib(512)).toBe("512 MiB");
    expect(mib(2048)).toBe("2.0 GiB");
  });
});

describe("ms", () => {
  it("switches to seconds past a second", () => {
    expect(ms(850)).toBe("850ms");
    expect(ms(2500)).toBe("2.5s");
  });
});

describe("titleCase", () => {
  it("turns a stage id into a label", () => {
    expect(titleCase("safety_gate")).toBe("Safety Gate");
    expect(titleCase("post_measure")).toBe("Post Measure");
  });
});
