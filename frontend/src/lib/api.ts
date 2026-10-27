/** Typed client for the dashboard API. Every call hits a real FastAPI endpoint; there are no
 *  fixtures here. Errors carry the backend's own detail so views can show what went wrong. */

import type {
  ActionList, LegacyCycleDetail, LegacyCycleList, PolicyList, Run, RunEvents, RunList,
  ScenarioList, Snapshot, StopResult,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** 409 from POST /api/runs: another run is already active. */
  get isConflict() { return this.status === 409; }
  /** 503 from /api/snapshot while the sampler fills its first window. */
  get isWarmingUp() { return this.status === 503; }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers },
    });
  } catch (cause) {
    throw new ApiError(0, "Cannot reach the ResourceTuner backend", cause);
  }
  const text = await res.text();
  let body: unknown = undefined;
  if (text) {
    try { body = JSON.parse(text); } catch { body = text; }
  }
  if (!res.ok) {
    const detail = (body as { detail?: unknown } | undefined)?.detail ?? body;
    throw new ApiError(res.status, messageFor(res.status, detail), detail);
  }
  return body as T;
}

function messageFor(status: number, detail: unknown): string {
  if (detail && typeof detail === "object" && "message" in detail) return String((detail as { message: unknown }).message);
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { loc?: unknown[]; msg?: string };
    return `${(first.loc ?? []).slice(1).join(".")}: ${first.msg ?? "invalid value"}`;
  }
  return `Request failed (HTTP ${status})`;
}

export interface StartRunBody {
  scenario: string;
  level?: string | null;
  cycles?: number;
  cooldown_s?: number;
  baseline_duration?: number;
  trigger_wait_timeout_s?: number;
  start_scenario?: boolean;
}

export const api = {
  snapshot: () => request<Snapshot>("/api/snapshot"),
  scenarios: () => request<ScenarioList>("/api/scenarios"),

  runs: (params: { limit?: number; offset?: number; status?: string } = {}) =>
    request<RunList>(`/api/runs?${new URLSearchParams(clean(params))}`),
  activeRun: () => request<{ active: Run | null }>("/api/runs/active"),
  run: (runId: string) => request<Run>(`/api/runs/${encodeURIComponent(runId)}`),
  startRun: (body: StartRunBody) =>
    request<Run>("/api/runs", { method: "POST", body: JSON.stringify(body) }),
  stopRun: (runId: string) =>
    request<StopResult>(`/api/runs/${encodeURIComponent(runId)}/stop`, { method: "POST" }),
  runEvents: (runId: string, params: { after_seq?: number; limit?: number; cycle?: number } = {}) =>
    request<RunEvents>(`/api/runs/${encodeURIComponent(runId)}/events?${new URLSearchParams(clean(params))}`),

  historyCycles: (params: { limit?: number; offset?: number } = {}) =>
    request<LegacyCycleList>(`/api/history/cycles?${new URLSearchParams(clean(params))}`),
  historyCycle: (cycle: number) => request<LegacyCycleDetail>(`/api/history/cycles/${cycle}`),
  historyActions: (params: { limit?: number; offset?: number } = {}) =>
    request<ActionList>(`/api/history/actions?${new URLSearchParams(clean(params))}`),
  historyPolicies: () => request<PolicyList>("/api/history/policies"),
};

function clean(params: Record<string, unknown>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null).map(([k, v]) => [k, String(v)]),
  );
}

/** SSE URL for live stage events. `after_seq` is the resume cursor; the browser also sends
 *  Last-Event-ID automatically on reconnect, and the backend honours both. */
export function eventStreamUrl(opts: { runId?: string; afterSeq?: number } = {}): string {
  const q = new URLSearchParams();
  if (opts.runId) q.set("run_id", opts.runId);
  if (opts.afterSeq) q.set("after_seq", String(opts.afterSeq));
  const qs = q.toString();
  return `/api/events/stream${qs ? `?${qs}` : ""}`;
}
