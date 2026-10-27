/** Formatting helpers. A null metric is "—", never 0: the difference matters on this dashboard. */

export const DASH = "—";

export function num(value: number | null | undefined, digits = 1, suffix = ""): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return `${value.toFixed(digits)}${suffix}`;
}

export function pct(value: number | null | undefined, digits = 1): string {
  return num(value, digits, "%");
}

export function signedPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

export function seconds(value: number | null | undefined, digits = 2): string {
  return num(value, digits, "s");
}

export function mib(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return DASH;
  return value >= 1024 ? `${(value / 1024).toFixed(1)} GiB` : `${value.toFixed(digits)} MiB`;
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return DASH;
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
}

export function clockTime(epoch: number | null | undefined): string {
  if (!epoch) return DASH;
  return new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function dateTime(epoch: number | null | undefined): string {
  if (!epoch) return DASH;
  return new Date(epoch * 1000).toLocaleString([], {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

export function relativeTime(epoch: number | null | undefined, now = Date.now()): string {
  if (!epoch) return DASH;
  const s = Math.max(0, Math.round(now / 1000 - epoch));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function duration(from: number | null | undefined, to: number | null | undefined): string {
  if (!from) return DASH;
  const end = to ?? Date.now() / 1000;
  const s = Math.max(0, Math.round(end - from));
  const m = Math.floor(s / 60);
  return m ? `${m}m ${s % 60}s` : `${s}s`;
}

/** Turns a stage id into a readable label: "safety_gate" -> "Safety Gate". */
export function titleCase(id: string): string {
  return id.split(/[_\s]+/).map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
}
