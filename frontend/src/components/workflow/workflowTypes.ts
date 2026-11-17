/** Types for the Agent Workflow interactive visualization.
 *
 *  Reflects the actual multi-agent architecture:
 *  - Google ADK Agents: Supervisor, Decision, Meta
 *  - CrewAI Specialists: CPU, RAM, I/O
 *  - Deterministic Systems: Telemetry/Trigger, Classifier, Action Guard, Safety Gate,
 *    Controller, Evaluation Engine, Policy Store.
 */

import type { StageStatus } from "@/lib/types";

export type AgentFramework = "Google ADK" | "CrewAI" | "Deterministic" | "Linux Kernel";

export type WorkflowNodeCategory =
  | "agent_supervisor"
  | "agent_specialist"
  | "agent_decision"
  | "agent_meta"
  | "system_input"
  | "system_classifier"
  | "system_guard"
  | "system_gate"
  | "system_actuator"
  | "system_eval"
  | "system_store";

export interface WorkflowNodeData extends Record<string, unknown> {
  id: string;
  name: string;
  category: WorkflowNodeCategory;
  framework: AgentFramework;
  model?: string;
  responsibility: string;
  status: StageStatus;
  durationMs: number | null;
  inputSummary?: string;
  outputSummary?: string;
  operationsSummary?: string;
  upstreamIds: string[];
  downstreamIds: string[];
  rawInput?: unknown;
  rawOutput?: unknown;
  rawProcessing?: unknown;
  provenance?: {
    provider?: string;
    model?: string;
    attempts?: number;
    tokens?: { prompt?: number; completion?: number; total?: number };
    degraded?: boolean;
  };
  checks?: {
    ok?: boolean;
    issues?: string[];
  };
  a2aState?: "SUBMITTED" | "WORKING" | "COMPLETED" | null;
  error?: string | null;
  isSelected?: boolean;
  isExecuting?: boolean;
}

export interface WorkflowTimelineEvent {
  id: string;
  timestamp: number;
  timeFormatted: string;
  type: "start" | "input" | "processing" | "output" | "handoff" | "check" | "rollback" | "error";
  sourceNodeId: string;
  sourceName: string;
  targetNodeId?: string;
  targetName?: string;
  title: string;
  description: string;
  status: StageStatus;
  payloadSnippet?: string;
  rawPayload?: unknown;
}
