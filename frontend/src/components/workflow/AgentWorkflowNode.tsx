/** Custom React Flow Node for the Agent Workflow visualization.
 *
 *  Visualizes individual agents and deterministic modules with:
 *  - Framework branding (Google ADK, CrewAI, Deterministic, Linux Kernel)
 *  - Real-time execution status (Running, Completed, Waiting, Failed, Skipped)
 *  - Model provenance tag
 *  - Input/Output summary snippets
 *  - Latency / Execution duration badge
 */

import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import {
  Bot, Terminal, CheckCircle2, Clock, AlertCircle, Sparkles,
} from "lucide-react";
import type { WorkflowNodeData, AgentFramework } from "./workflowTypes";
import { STAGE_STATUS_META } from "@/lib/status";
import { ms } from "@/lib/format";
import { cn } from "@/lib/utils";

const FRAMEWORK_THEMES: Record<AgentFramework, { border: string; bg: string; text: string; badge: string }> = {
  "Google ADK": {
    border: "border-blue-500/50",
    bg: "bg-blue-950/20",
    text: "text-blue-400",
    badge: "bg-blue-500/10 text-blue-400 border-blue-500/30",
  },
  "CrewAI": {
    border: "border-emerald-500/50",
    bg: "bg-emerald-950/20",
    text: "text-emerald-400",
    badge: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30",
  },
  "Deterministic": {
    border: "border-purple-500/40",
    bg: "bg-purple-950/20",
    text: "text-purple-400",
    badge: "bg-purple-500/10 text-purple-400 border-purple-500/30",
  },
  "Linux Kernel": {
    border: "border-amber-500/40",
    bg: "bg-amber-950/20",
    text: "text-amber-400",
    badge: "bg-amber-500/10 text-amber-400 border-amber-500/30",
  },
};

export function AgentWorkflowNode({ data }: NodeProps<Node<WorkflowNodeData>>) {
  const statusMeta = STAGE_STATUS_META[data.status] || STAGE_STATUS_META.waiting;
  const frameworkTheme = FRAMEWORK_THEMES[data.framework] || FRAMEWORK_THEMES.Deterministic;
  const isAgent = data.category.startsWith("agent_");
  const isRunning = data.status === "running";
  const isCompleted = data.status === "completed";
  const isFailed = data.status === "failed" || data.status === "rejected";

  return (
    <div
      className={cn(
        "group relative flex w-[220px] flex-col rounded-xl border bg-panel p-3 text-left shadow-md transition-all duration-200",
        data.isSelected ? "ring-2 ring-accent border-accent shadow-accent/20 shadow-lg" : "hover:border-border-strong",
        isRunning && "ring-2 ring-accent animate-pulse border-accent",
        isCompleted && "border-ok/40",
        isFailed && "border-bad/60",
      )}
      style={{
        boxShadow: isRunning ? `0 0 16px color-mix(in oklab, ${statusMeta.color} 35%, transparent)` : undefined,
      }}
    >
      {/* Target Handle */}
      <Handle
        type="target"
        position={Position.Left}
        className="!size-2.5 !border-2 !border-bg !bg-border-strong transition-colors group-hover:!bg-accent"
      />

      {/* Top Header: Framework & Status */}
      <div className="flex items-center justify-between gap-1.5 pb-2 border-b border-border/50">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className={cn("grid size-5 shrink-0 place-items-center rounded", frameworkTheme.bg, frameworkTheme.text)}>
            {isAgent ? <Bot className="size-3.5" /> : <Terminal className="size-3.5" />}
          </span>
          <span className={cn("truncate rounded border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider", frameworkTheme.badge)}>
            {data.framework}
          </span>
        </div>

        {/* Live Status indicator */}
        <div className="flex items-center gap-1 shrink-0">
          {isRunning ? (
            <span className="flex size-2 relative">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-accent opacity-75" />
              <span className="relative inline-flex rounded-full size-2 bg-accent" />
            </span>
          ) : isCompleted ? (
            <CheckCircle2 className="size-3 text-ok" />
          ) : isFailed ? (
            <AlertCircle className="size-3 text-bad" />
          ) : (
            <span className="size-2 rounded-full bg-fg-subtle/40" />
          )}
          <span className="text-[10px] font-medium" style={{ color: statusMeta.color }}>
            {statusMeta.label}
          </span>
        </div>
      </div>

      {/* Node Body: Name & Model */}
      <div className="pt-2">
        <div className="flex items-start justify-between gap-1">
          <h4 className="text-xs font-semibold leading-tight text-fg truncate" title={data.name}>
            {data.name}
          </h4>
        </div>

        {data.model && (
          <div className="mt-0.5 flex items-center gap-1 text-[10px] text-fg-subtle truncate" title={data.model}>
            <Sparkles className="size-2.5 text-accent-strong shrink-0" />
            <span className="truncate">{data.model.replace("groq/openai/", "")}</span>
          </div>
        )}

        <p className="mt-1 line-clamp-2 text-[10px] leading-snug text-fg-muted" title={data.responsibility}>
          {data.responsibility}
        </p>
      </div>

      {/* Input / Output Snippets */}
      <div className="mt-2.5 space-y-1 rounded-md bg-bg-raised/60 p-1.5 text-[9px]">
        {data.inputSummary && (
          <div className="flex items-center gap-1 text-fg-muted truncate">
            <span className="font-semibold text-fg-subtle shrink-0">IN:</span>
            <span className="truncate">{data.inputSummary}</span>
          </div>
        )}
        {data.outputSummary && (
          <div className="flex items-center gap-1 text-fg-muted truncate">
            <span className="font-semibold text-accent shrink-0">OUT:</span>
            <span className="truncate text-fg">{data.outputSummary}</span>
          </div>
        )}
      </div>

      {/* Footer: Duration & A2A State */}
      <div className="mt-2 flex items-center justify-between text-[9px] text-fg-subtle pt-1.5 border-t border-border/40">
        <div className="flex items-center gap-1">
          {data.a2aState ? (
            <span className="rounded bg-accent/10 px-1 py-0.2 font-mono text-[8px] text-accent uppercase font-bold">
              A2A: {data.a2aState}
            </span>
          ) : (
            <span className="text-[8px] uppercase tracking-wider text-fg-subtle">
              {data.category.replace("_", " ")}
            </span>
          )}
        </div>

        {data.durationMs !== null && (
          <div className="flex items-center gap-0.5 nums font-mono text-fg-muted">
            <Clock className="size-2.5" />
            <span>{ms(data.durationMs)}</span>
          </div>
        )}
      </div>

      {/* Source Handle */}
      <Handle
        type="source"
        position={Position.Right}
        className="!size-2.5 !border-2 !border-bg !bg-border-strong transition-colors group-hover:!bg-accent"
      />
    </div>
  );
}
