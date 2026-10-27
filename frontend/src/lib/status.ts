/** One mapping from backend vocabulary to how it looks and reads, used by every view so the
 *  same state never appears in two different colours. Mirrors resourcetuner/pipeline/stages.py. */

import type { BadgeTone } from "@/components/ui/badge";
import type { RunStatus, StageStatus } from "./types";

export const CYCLE_STAGES = [
  "baseline", "trigger", "classify", "supervisor", "diagnostics", "decision", "safety_gate",
  "controller", "post_measure", "evaluation", "policy", "meta", "cycle_summary",
] as const;
export const RUN_LEVEL_STAGES = ["scenario_start", "warmup", "run_summary"] as const;

/** The pipeline as the brief draws it: telemetry → … → policy store. */
export const PIPELINE_ORDER = [
  "trigger", "classify", "supervisor", "diagnostics", "decision", "safety_gate",
  "controller", "post_measure", "evaluation", "meta", "policy",
] as const;

export const STAGE_LABELS: Record<string, string> = {
  scenario_start: "Scenario Start", warmup: "Warm-up Build", run_summary: "Run Summary",
  baseline: "Baseline Measure", trigger: "Contention Trigger", classify: "Workload Classifier",
  supervisor: "ADK Supervisor", diagnostics: "CrewAI Diagnostics", decision: "Decision Agent",
  safety_gate: "Safety Gate", controller: "MCP / Controller", post_measure: "Post-action Measure",
  evaluation: "Evaluation Engine", policy: "Policy Store", meta: "Meta-Agent",
  cycle_summary: "Cycle Summary",
};

export const STAGE_SUBTITLES: Record<string, string> = {
  trigger: "deterministic threshold",
  classify: "deterministic rules",
  supervisor: "Google ADK · Gemini",
  diagnostics: "CrewAI · CPU / RAM / I/O",
  decision: "Google ADK · Gemini",
  safety_gate: "deterministic · 10 checks",
  controller: "cgroup write + readback",
  post_measure: "measured build",
  evaluation: "±10% discrete delta",
  meta: "Google ADK · promotion",
  policy: "SQLite · learned stats",
};

export const STAGE_STATUS_META: Record<StageStatus, { label: string; tone: BadgeTone; color: string }> = {
  waiting: { label: "Waiting", tone: "neutral", color: "var(--color-fg-subtle)" },
  running: { label: "Running", tone: "accent", color: "var(--color-accent)" },
  completed: { label: "Completed", tone: "ok", color: "var(--color-ok)" },
  rejected: { label: "Rejected", tone: "bad", color: "var(--color-bad)" },
  skipped: { label: "Skipped", tone: "idle", color: "var(--color-idle)" },
  failed: { label: "Failed", tone: "bad", color: "var(--color-bad)" },
  evidence_unavailable: { label: "Evidence unavailable", tone: "unknown", color: "var(--color-unknown)" },
};

export const RUN_STATUS_META: Record<RunStatus, { label: string; tone: BadgeTone }> = {
  requested: { label: "Requested", tone: "neutral" },
  running: { label: "Running", tone: "accent" },
  completed: { label: "Completed", tone: "ok" },
  failed: { label: "Failed", tone: "bad" },
  cancelled: { label: "Cancelled", tone: "warn" },
};

/** Workload identity colours.
 *
 *  Assigned in this FIXED order and never cycled: a class keeps its hue no matter how many
 *  classes a chart happens to show, so a filter that drops a series never repaints the rest.
 *
 *  Validated with the dataviz validator against the panel surface #1b1e25 (dark mode):
 *
 *  * the five-slot ordered set PASSES all six checks on the *adjacent* pairlist
 *    (worst adjacent CVD ΔE 8.4 protan, worst adjacent normal-vision ΔE 19.3) — so bars, the
 *    stacked class-activity breakdown and the workload timeline strip may show all five,
 *    provided they keep this order and carry direct labels;
 *  * on *all* pairs only the first three (#d95926, #3987e5, #199e70) pass
 *    (worst CVD ΔE 9.4 deutan, worst normal-vision ΔE 20.9). BUILD, TESTING and CODE_INDEXING
 *    are exactly the three classes our scenarios actually produce together, so scatter-like
 *    forms where any two marks may be compared are capped at those three; a fourth folds into
 *    "Other" rather than taking a new hue.
 *
 *  IDLE and BACKGROUND are deliberately neutral greys: they are states, not identities, and
 *  must never compete with a real workload for attention.
 */
export const WORKLOAD_ORDER = ["BUILD", "TESTING", "CODE_INDEXING", "DEV_SERVER", "UNKNOWN"] as const;

/** The subset that is safe when any two marks may be compared (see WORKLOAD_ORDER). */
export const ALL_PAIRS_SAFE_SLOTS = 3;

export const WORKLOAD_COLORS: Record<string, string> = {
  BUILD: "var(--color-wl-build)",
  TESTING: "var(--color-wl-testing)",
  CODE_INDEXING: "var(--color-wl-indexing)",
  DEV_SERVER: "var(--color-wl-dev)",
  UNKNOWN: "var(--color-wl-unknown)",
  IDLE: "var(--color-idle)",
  BACKGROUND: "var(--color-fg-subtle)",
};

/** Orders class keys by WORKLOAD_ORDER so every chart lays them out identically. */
export function orderWorkloads(keys: Iterable<string>): string[] {
  const rank = (k: string) => {
    const i = (WORKLOAD_ORDER as readonly string[]).indexOf(k);
    return i === -1 ? WORKLOAD_ORDER.length + (k === "IDLE" ? 2 : 1) : i;
  };
  return [...keys].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

export function workloadColor(workload: string | null | undefined): string {
  return (workload && WORKLOAD_COLORS[workload]) || "var(--color-fg-subtle)";
}

export function workloadTone(workload: string | null | undefined): BadgeTone {
  if (!workload) return "neutral";
  if (workload === "UNKNOWN") return "unknown";
  if (workload === "IDLE") return "idle";
  return "accent";
}

/** IMPROVED / UNCHANGED / DEGRADED / TIMEOUT / ERROR from the Evaluation Engine. */
export function verdictTone(verdict: string | null | undefined): BadgeTone {
  switch (verdict) {
    case "IMPROVED": return "ok";
    case "UNCHANGED": return "warn";
    case "DEGRADED": case "TIMEOUT": case "ERROR": return "bad";
    default: return "neutral";
  }
}

export function executionTone(status: string | null | undefined): BadgeTone {
  switch (status) {
    case "applied": return "ok";
    case "rejected": case "blocked": return "bad";
    case "no_action": return "idle";
    case "target_gone": case "apply_failed": case "error": return "bad";
    default: return "neutral";
  }
}

/** Action lifecycle from data/actions.db. */
export function actionStatusTone(status: string | null | undefined): BadgeTone {
  switch (status) {
    case "kept": case "applied": return "ok";
    case "rolled_back": return "warn";
    case "pending": return "accent";
    case "rollback_failed": case "apply_failed": return "bad";
    case "target_gone": return "unknown";
    default: return "neutral";
  }
}

export const INTERVENTION_META: Record<string, { label: string; tone: BadgeTone }> = {
  supported: { label: "Optimization supported", tone: "ok" },
  pause_only: { label: "Pause only", tone: "warn" },
  observation_only: { label: "Observation only", tone: "unknown" },
  none: { label: "No actionable target", tone: "idle" },
  unknown: { label: "Unknown", tone: "neutral" },
};
