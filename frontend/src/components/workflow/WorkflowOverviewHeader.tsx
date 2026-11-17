/** Execution Overview Header for the Agent Workflow visualization.
 *
 *  Displays:
 *  - Workflow status
 *  - Run ID & scenario name
 *  - Cycle number
 *  - Agent completion statistics (Completed / Running / Pending / Failed)
 *  - Run selector and live indicator
 */

import { Activity, CheckCircle2, Clock, AlertCircle, Bot, Zap } from "lucide-react";
import type { RunSelection } from "@/components/RunPicker";
import { Badge } from "@/components/ui/badge";
import { RUN_STATUS_META } from "@/lib/status";
import { cn } from "@/lib/utils";

interface WorkflowOverviewHeaderProps {
  sel: RunSelection;
  agentCounts: {
    total: number;
    completed: number;
    running: number;
    pending: number;
    failed: number;
    skipped: number;
  };
}

export function WorkflowOverviewHeader({ sel, agentCounts }: WorkflowOverviewHeaderProps) {
  const status = sel.run?.status ?? "requested";
  const statusMeta = RUN_STATUS_META[status] || RUN_STATUS_META.requested;
  const isLive = sel.live;

  return (
    <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-border bg-panel p-4 shadow-sm">
      {/* Left: Execution Status & Run info */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <div className={cn("grid size-9 place-items-center rounded-lg border", isLive ? "bg-accent/10 border-accent/30 text-accent animate-pulse" : "bg-panel-hover border-border text-fg-muted")}>
            <Zap className="size-5" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-fg">
                {sel.run ? `Run: ${sel.run.run_id}` : "No Active Run"}
              </h3>
              <Badge tone={statusMeta.tone}>
                {isLive ? "LIVE EXECUTION" : statusMeta.label}
              </Badge>
              {sel.cycle !== null && (
                <span className="rounded bg-bg-raised px-2 py-0.5 text-xs font-medium text-fg-muted border border-border">
                  Cycle {sel.cycle}
                </span>
              )}
            </div>
            <p className="text-xs text-fg-subtle">
              {sel.run?.scenario ? `Scenario: ${sel.run.scenario} (${sel.run.level || "default"})` : "End-to-End AI Agent Pipeline & Data Flow"}
            </p>
          </div>
        </div>
      </div>

      {/* Right: Agent Completion Stats Pills */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-1.5 rounded-lg border border-border bg-bg-raised/70 px-2.5 py-1.5 text-xs">
          <Bot className="size-3.5 text-blue-400" />
          <span className="text-fg-subtle">Agents:</span>
          <span className="font-semibold text-fg">{agentCounts.total}</span>
        </div>

        <div className="flex items-center gap-1.5 rounded-lg border border-border bg-bg-raised/70 px-2.5 py-1.5 text-xs">
          <CheckCircle2 className="size-3.5 text-ok" />
          <span className="text-fg-subtle">Completed:</span>
          <span className="font-semibold text-ok">{agentCounts.completed}</span>
        </div>

        {agentCounts.running > 0 && (
          <div className="flex items-center gap-1.5 rounded-lg border border-accent/40 bg-accent/10 px-2.5 py-1.5 text-xs">
            <Activity className="size-3.5 text-accent animate-spin" />
            <span className="text-accent font-medium">Running:</span>
            <span className="font-bold text-accent">{agentCounts.running}</span>
          </div>
        )}

        {agentCounts.pending > 0 && (
          <div className="flex items-center gap-1.5 rounded-lg border border-border bg-bg-raised/70 px-2.5 py-1.5 text-xs">
            <Clock className="size-3.5 text-fg-subtle" />
            <span className="text-fg-subtle">Waiting:</span>
            <span className="font-semibold text-fg-muted">{agentCounts.pending}</span>
          </div>
        )}

        {agentCounts.failed > 0 && (
          <div className="flex items-center gap-1.5 rounded-lg border border-bad/40 bg-bad/10 px-2.5 py-1.5 text-xs">
            <AlertCircle className="size-3.5 text-bad" />
            <span className="text-bad font-medium">Failed/Blocked:</span>
            <span className="font-bold text-bad">{agentCounts.failed}</span>
          </div>
        )}
      </div>
    </div>
  );
}
