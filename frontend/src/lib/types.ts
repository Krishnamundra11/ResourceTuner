/** Types mirroring the FastAPI responses in resourcetuner/dashboard/{api,main,history}.py.
 *  Anything optional here is optional on the backend too: a missing metric must render as
 *  "unavailable", never as 0. */

// --- live telemetry (GET /api/snapshot) ---------------------------------------------------

export interface HostMetrics {
  cpu_percent: number;
  cores: number;
  iowait_percent: number;
  load_1m: number;
  memory_percent: number;
  memory_available_mib: number;
  memory_total_mib: number;
  disk_read_mib_s: number;
  disk_write_mib_s: number;
  disk_util_percent: number | null;
  delayacct_enabled: boolean | null;
  psi_cpu: number | null;
  psi_memory: number | null;
  psi_io: number | null;
}

export interface ClassificationView {
  workload: string;
  confidence: number | null;
  dominance: number | null;
  persistence: number | null;
  needs_llm_fallback: boolean;
  fallback_reasons: string[];
  background_activity: string[];
  unattributed_activity_percent: number;
  unattributed_processes: string[];
  class_activity_percent: Record<string, number>;
  class_io_wait_percent: Record<string, number>;
  window: number;
}

export interface ContainerRow {
  name: string;
  image: string;
  cpu_percent: number | null;
  memory_mib: number | null;
  memory_limit_mib: number | null;
  io_read_mib_s: number;
  io_write_mib_s: number;
  classified_as: string | null;
  /** Ground truth, shown for validation only — the classifier never reads it. */
  ground_truth_label: string | null;
}

export interface HistoryPoint { t: number; cpu: number; mem: number; workload: string }

export interface Snapshot {
  timestamp: number;
  host: HostMetrics;
  classification: ClassificationView | null;
  containers: ContainerRow[];
  docker: { available: boolean; error: string | null };
  history: HistoryPoint[];
  sampler_error?: string | null;
}

// --- scenarios (GET /api/scenarios) ------------------------------------------------------

export type Intervention = "supported" | "pause_only" | "observation_only" | "none" | "unknown";

export interface Scenario {
  name: string;
  description: string;
  levels: string[];
  default_level: string | null;
  containers: Record<string, string>;
  workloads: string[];
  resource_dimensions: string[];
  intervention: Intervention;
  intervention_note: string;
  error?: string;
}

export interface ScenarioList { scenarios: Scenario[]; max_cycles: number }

// --- runs and events ---------------------------------------------------------------------

export type RunStatus = "requested" | "running" | "completed" | "failed" | "cancelled";

export interface Run {
  run_id: string;
  kind: "cli" | "dashboard";
  scenario: string | null;
  level: string | null;
  cycles_requested: number;
  cycles_done: number;
  status: RunStatus;
  params: Record<string, unknown>;
  created_at: number;
  started_at: number | null;
  ended_at: number | null;
  error: string | null;
  active: boolean;
  thread_alive?: boolean;
  stop_requested?: boolean;
  cycles_seen?: number[];
  last_seq?: number;
}

export interface RunList { runs: Run[]; total: number; limit: number; offset: number }

export type StageStatus =
  | "waiting" | "running" | "completed" | "rejected" | "skipped" | "failed" | "evidence_unavailable";

export interface StageEvent {
  seq: number;
  run_id: string;
  cycle: number | null;
  stage: string;
  status: StageStatus;
  ts: number;
  duration_ms: number | null;
  summary: string;
  payload: Record<string, unknown>;
}

export interface RunEvents {
  run_id: string;
  events: StageEvent[];
  after_seq: number;
  next_seq: number;
  stages: string[];
  statuses: StageStatus[];
}

export interface StopResult {
  run_id: string;
  status: RunStatus;
  stop_requested: boolean;
  detail: string;
}

// --- read-only history --------------------------------------------------------------------

export interface LegacyAction { tool: string; target_id: string; weight?: number | null; cpus?: number | null }

export interface LegacyCycleSummary {
  cycle: number;
  timestamp: number | null;
  duration_s: number | null;
  trigger: { resource?: string; metric?: string; value?: number; threshold?: number; persisted_s?: number } | null;
  delegated_to: string[] | null;
  candidate_count: number;
  action: LegacyAction | null;
  execution_status: string | null;
  gate_reasons: string[] | null;
  verdict: string | null;
  improvement_pct: number | null;
  rolled_back: boolean | null;
  before_s: number | null;
  after_s: number | null;
  llm_degraded: boolean | null;
  promote: boolean | null;
}

export interface ReplayStage {
  stage: string;
  status: StageStatus;
  summary: string;
  payload: Record<string, unknown>;
  ts: number | null;
  duration_ms: number | null;
  derived_from: "cycle_summary";
}

export interface LegacyCycleList {
  source: string; read_only: true; note: string;
  total: number; limit: number; offset: number; cycles: LegacyCycleSummary[];
}

export interface LegacyCycleDetail {
  source: string; read_only: true; cycle: number;
  summary: LegacyCycleSummary; stages: ReplayStage[]; raw: Record<string, unknown>;
}

export interface ActionRecord {
  action_id: string; tool: string; target_id: string; container_id: string; container_name: string;
  knob: string; old_value: unknown; new_value: unknown; status: string;
  created_at: number; applied_at: number | null; resolved_at: number | null; detail: string | null;
}

export interface ActionList {
  source: string; read_only: true; total: number; limit: number; offset: number; actions: ActionRecord[];
}

export interface PolicyRecord {
  workload: string; condition: string; action_tool: string; action_target: string;
  action_param: Record<string, number> | string;
  success_count: number; failure_count: number; avg_improvement: number | null; sample_count: number;
  promoted: boolean; last_outcome: string | null; last_updated: number; success_rate: number | null;
}

export interface PolicyList {
  source: string; read_only: true; total: number; policies: PolicyRecord[];
  promotion_floor: { min_samples: number; min_success_rate: number; requires_positive_improvement: boolean };
}

// --- stage payload shapes -----------------------------------------------------------------
// These mirror the dataclasses and Pydantic models the pipeline puts into an event payload.
// Every field is optional: a payload is evidence, and an older run may simply not carry it.

export interface EvidenceItem { metric: string; value: number; statement: string }

export interface CandidateAction {
  tool: string; target_id: string;
  weight?: number | null; cpus?: number | null; rationale?: string;
}

export interface Diagnosis {
  resource: "cpu" | "memory" | "io";
  contention: boolean;
  /** RAM only: whether the pressure is host-wide or inside one container's own limit. */
  scope?: "host" | "container" | "none";
  summary: string;
  culprit_target_ids: string[];
  evidence: EvidenceItem[];
  candidate_action?: CandidateAction | null;
  historical_basis?: string | null;
}

export interface Provenance {
  provider?: string; model?: string; degraded?: boolean; attempts?: unknown[];
}

/** One diagnostic agent's answer, as the CrewAI crew returns it through the Supervisor. */
export interface Finding {
  agent: string;
  diagnosis: Diagnosis | null;
  check_ok: boolean;
  check_issues: string[];
  provenance: Provenance;
  error?: string | null;
  extra?: Record<string, unknown>;
}

export interface Delegation { resources?: string[]; reason?: string }

export interface DecisionPayload {
  chosen_action?: CandidateAction | null;
  reasoning?: string;
  considered?: unknown[];
}

export interface ExecutionPayload {
  status?: string;
  reasons?: string[];
  action_id?: string | null;
  knob?: string;
  old_value?: unknown;
  new_value?: unknown;
  observed?: unknown;
  checks?: Record<string, unknown>;
  tool?: string;
  target_id?: string;
}

export interface EvaluationPayload {
  verdict?: string;
  improvement_pct?: number | null;
  detail?: string;
  rolled_back?: boolean;
  before?: unknown;
  after?: unknown;
  cgroup_readback?: unknown;
}

export interface ClassifyPayload {
  workload?: { class?: string; confidence?: number; dominance?: number; persistence?: number;
               needs_llm_fallback?: boolean; fallback_reasons?: string[] };
  host?: Record<string, number | null>;
  targets?: { target_id?: string; workload?: string; cpu_percent?: number;
              cpu_weight?: number; state?: string }[];
}

export interface PolicyRecordPayload {
  workload?: string; condition?: string; action_tool?: string; action_target?: string;
  success_count?: number; failure_count?: number; sample_count?: number;
  avg_improvement?: number | null; last_outcome?: string; promoted?: boolean;
  preferred_action?: unknown;
}

export interface MetaPayload { promote?: boolean; reasoning?: string }

export interface CycleSummaryPayload {
  cycle?: number; status?: string; duration_s?: number;
  decision?: DecisionPayload; execution?: ExecutionPayload; evaluation?: EvaluationPayload;
  meta?: MetaPayload; error?: string | null;
  indexer_initial_weights?: unknown; indexer_post_action_weights?: unknown;
  indexer_final_weights?: unknown;
}
