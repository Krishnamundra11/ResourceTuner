/** Detailed Inspector Drawer / Panel for the selected Workflow Agent / Node.
 *
 *  Provides 5 comprehensive tabs/sections:
 *  A. Agent Information (Framework, Model, Status, Duration)
 *  B. Input (Exact payload, upstream source, parameters)
 *  C. Processing (Operations, Tools invoked, A2A Lifecycle transitions)
 *  D. Output (Generated findings, Pydantic schemas, downstream consumers)
 *  E. Execution Details (Checks verification, Hallucination audits, Tokens, Errors)
 */

import { useState } from "react";
import {
  X, Bot, Terminal, ShieldCheck, AlertCircle,
} from "lucide-react";
import type { WorkflowNodeData } from "./workflowTypes";
import { STAGE_STATUS_META } from "@/lib/status";
import { ms } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface AgentInspectorDrawerProps {
  node: WorkflowNodeData | null;
  onClose: () => void;
}

export function AgentInspectorDrawer({ node, onClose }: AgentInspectorDrawerProps) {
  const [activeTab, setActiveTab] = useState<"overview" | "input" | "processing" | "output" | "execution">("overview");

  if (!node) return null;

  const statusMeta = STAGE_STATUS_META[node.status] || STAGE_STATUS_META.waiting;
  const isAgent = node.category.startsWith("agent_");

  return (
    <aside className="fixed inset-y-0 right-0 z-50 flex w-full max-w-[480px] flex-col border-l border-border bg-bg-raised shadow-2xl transition-transform sm:w-[480px]">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border p-4 bg-panel">
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="grid size-9 shrink-0 place-items-center rounded-lg bg-accent-dim/30 text-accent-strong border border-accent/20">
            {isAgent ? <Bot className="size-5" /> : <Terminal className="size-5" />}
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="truncate text-sm font-semibold text-fg">{node.name}</h3>
              <Badge tone={statusMeta.tone}>{statusMeta.label}</Badge>
            </div>
            <p className="text-[11px] text-fg-subtle truncate">
              {node.framework} {node.model ? `· ${node.model}` : ""}
            </p>
          </div>
        </div>

        <button
          onClick={onClose}
          className="rounded-lg p-1.5 text-fg-subtle hover:bg-panel-hover hover:text-fg transition-colors"
          aria-label="Close inspector"
        >
          <X className="size-4" />
        </button>
      </div>

      {/* Navigation Tabs */}
      <div className="flex border-b border-border bg-panel/50 px-2 text-xs font-medium text-fg-muted">
        {[
          { id: "overview", label: "Overview" },
          { id: "input", label: "Input" },
          { id: "processing", label: "Processing" },
          { id: "output", label: "Output" },
          { id: "execution", label: "Execution" },
        ].map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id as typeof activeTab)}
            className={cn(
              "border-b-2 px-3 py-2.5 transition-colors",
              activeTab === tab.id
                ? "border-accent text-accent-strong font-semibold"
                : "border-transparent hover:text-fg hover:border-border",
            )}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Scrollable Content Body */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* TAB 1: OVERVIEW */}
        {activeTab === "overview" && (
          <div className="space-y-4 text-xs">
            <div className="rounded-xl border border-border bg-panel p-3.5 space-y-2.5">
              <h4 className="font-semibold text-fg text-xs uppercase tracking-wider text-fg-subtle">
                A. Agent Information
              </h4>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div>
                  <span className="text-fg-subtle">Framework:</span>
                  <p className="font-medium text-fg mt-0.5">{node.framework}</p>
                </div>
                <div>
                  <span className="text-fg-subtle">Architecture Type:</span>
                  <p className="font-medium text-fg mt-0.5">{node.category.replace("_", " ").toUpperCase()}</p>
                </div>
                {node.model && (
                  <div className="col-span-2">
                    <span className="text-fg-subtle">Primary Model:</span>
                    <p className="font-mono text-fg-muted mt-0.5 bg-bg-raised p-1.5 rounded border border-border">
                      {node.model}
                    </p>
                  </div>
                )}
                <div>
                  <span className="text-fg-subtle">Execution Duration:</span>
                  <p className="font-mono text-fg mt-0.5">{node.durationMs !== null ? ms(node.durationMs) : "Waiting / In progress"}</p>
                </div>
                <div>
                  <span className="text-fg-subtle">Current Status:</span>
                  <p className="font-medium capitalize mt-0.5" style={{ color: statusMeta.color }}>
                    {statusMeta.label}
                  </p>
                </div>
              </div>

              <div className="pt-2 border-t border-border/50">
                <span className="text-fg-subtle">Primary Responsibility:</span>
                <p className="text-fg-muted mt-1 leading-relaxed bg-bg-raised/60 p-2 rounded">
                  {node.responsibility}
                </p>
              </div>
            </div>

            {/* Dependency Graph Handoff summary */}
            <div className="rounded-xl border border-border bg-panel p-3.5 space-y-2">
              <h4 className="font-semibold text-xs uppercase tracking-wider text-fg-subtle">
                Data Handoff Boundaries
              </h4>
              <div className="space-y-2">
                <div>
                  <span className="text-fg-subtle text-[11px]">Upstream Sources (Receives from):</span>
                  <div className="flex flex-wrap gap-1.5 mt-1">
                    {node.upstreamIds.length > 0 ? (
                      node.upstreamIds.map((id) => (
                        <span key={id} className="rounded bg-bg-raised px-2 py-0.5 font-mono text-[11px] text-fg border border-border">
                          {id}
                        </span>
                      ))
                    ) : (
                      <span className="text-fg-subtle italic">None (Root Pipeline Input)</span>
                    )}
                  </div>
                </div>

                <div>
                  <span className="text-fg-subtle text-[11px]">Downstream Consumers (Passes to):</span>
                  <div className="flex flex-wrap gap-1.5 mt-1">
                    {node.downstreamIds.length > 0 ? (
                      node.downstreamIds.map((id) => (
                        <span key={id} className="rounded bg-bg-raised px-2 py-0.5 font-mono text-[11px] text-fg border border-border">
                          {id}
                        </span>
                      ))
                    ) : (
                      <span className="text-fg-subtle italic">None (Terminal Stage)</span>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* TAB 2: INPUT */}
        {activeTab === "input" && (
          <div className="space-y-4 text-xs">
            <div className="rounded-xl border border-border bg-panel p-3.5 space-y-2">
              <h4 className="font-semibold text-fg text-xs uppercase tracking-wider text-fg-subtle">
                B. Input Data & Context
              </h4>
              <p className="text-fg-muted leading-relaxed">
                {node.inputSummary || "No input recorded for this stage."}
              </p>
              <div className="pt-2 text-[11px] text-fg-subtle">
                Upstream Provider: <span className="font-mono text-fg">{node.upstreamIds.join(", ") || "Root System"}</span>
              </div>
            </div>

            <div className="rounded-xl border border-border bg-panel p-3.5 space-y-2">
              <span className="font-semibold text-xs uppercase tracking-wider text-fg-subtle">
                Raw Input Payload (JSON)
              </span>
              <pre className="overflow-x-auto rounded-lg bg-bg p-3 font-mono text-[11px] text-fg-muted max-h-72 border border-border">
                {node.rawInput ? JSON.stringify(node.rawInput, null, 2) : "// Awaiting input payload from upstream execution..."}
              </pre>
            </div>
          </div>
        )}

        {/* TAB 3: PROCESSING */}
        {activeTab === "processing" && (
          <div className="space-y-4 text-xs">
            <div className="rounded-xl border border-border bg-panel p-3.5 space-y-3">
              <h4 className="font-semibold text-fg text-xs uppercase tracking-wider text-fg-subtle">
                C. Internal Operations & Tools
              </h4>
              <p className="text-fg-muted leading-relaxed">
                {node.operationsSummary || "Standard stage execution."}
              </p>

              {node.a2aState && (
                <div className="rounded-lg border border-accent/30 bg-accent/10 p-3 space-y-1.5">
                  <div className="flex items-center justify-between">
                    <span className="font-semibold text-accent">A2A Protocol Task Lifecycle</span>
                    <span className="font-mono font-bold text-accent">{node.a2aState}</span>
                  </div>
                  <p className="text-[11px] text-fg-subtle">
                    State progression: SUBMITTED → WORKING → COMPLETED as per Linux Foundation A2A specification.
                  </p>
                </div>
              )}
            </div>

            {node.checks && (
              <div className="rounded-xl border border-border bg-panel p-3.5 space-y-2">
                <div className="flex items-center gap-1.5">
                  <ShieldCheck className={cn("size-4", node.checks.ok ? "text-ok" : "text-bad")} />
                  <span className="font-semibold text-xs text-fg">checks.py Deterministic Groundedness Verification</span>
                </div>
                <p className="text-[11px] text-fg-muted">
                  {node.checks.ok ? "All metric citations verified against input evidence (0 hallucinations)." : "Check issues detected during verification."}
                </p>
                {node.checks.issues && node.checks.issues.length > 0 && (
                  <ul className="list-disc pl-4 space-y-1 text-bad text-[11px]">
                    {node.checks.issues.map((iss, i) => (
                      <li key={i}>{iss}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        )}

        {/* TAB 4: OUTPUT */}
        {activeTab === "output" && (
          <div className="space-y-4 text-xs">
            <div className="rounded-xl border border-border bg-panel p-3.5 space-y-2">
              <h4 className="font-semibold text-fg text-xs uppercase tracking-wider text-fg-subtle">
                D. Output Generated & Handoff Destination
              </h4>
              <p className="text-fg leading-relaxed bg-bg-raised/70 p-2.5 rounded-lg border border-border">
                {node.outputSummary || "Stage has not generated output yet."}
              </p>
              <div className="pt-2 text-[11px] text-fg-subtle">
                Passed Downstream to: <span className="font-mono text-fg">{node.downstreamIds.join(", ") || "Terminal"}</span>
              </div>
            </div>

            <div className="rounded-xl border border-border bg-panel p-3.5 space-y-2">
              <span className="font-semibold text-xs uppercase tracking-wider text-fg-subtle">
                Raw Output Payload (Pydantic / JSON)
              </span>
              <pre className="overflow-x-auto rounded-lg bg-bg p-3 font-mono text-[11px] text-fg-muted max-h-72 border border-border">
                {node.rawOutput ? JSON.stringify(node.rawOutput, null, 2) : "// Awaiting output from execution..."}
              </pre>
            </div>
          </div>
        )}

        {/* TAB 5: EXECUTION DETAILS */}
        {activeTab === "execution" && (
          <div className="space-y-4 text-xs">
            <div className="rounded-xl border border-border bg-panel p-3.5 space-y-3">
              <h4 className="font-semibold text-fg text-xs uppercase tracking-wider text-fg-subtle">
                E. Provenance & Execution Details
              </h4>

              {node.provenance ? (
                <div className="space-y-2">
                  <div className="grid grid-cols-2 gap-2 text-[11px]">
                    <div>
                      <span className="text-fg-subtle">Provider:</span>
                      <p className="font-mono text-fg mt-0.5">{node.provenance.provider || "Gemini API"}</p>
                    </div>
                    <div>
                      <span className="text-fg-subtle">Degraded Fallback:</span>
                      <p className={cn("font-semibold mt-0.5", node.provenance.degraded ? "text-warn" : "text-ok")}>
                        {node.provenance.degraded ? "YES (Ollama local)" : "NO (Primary cloud model)"}
                      </p>
                    </div>
                  </div>
                </div>
              ) : (
                <p className="text-fg-subtle">Standard execution runtime telemetry.</p>
              )}

              {node.error && (
                <div className="rounded-lg border border-bad/30 bg-bad/10 p-3 text-bad space-y-1">
                  <span className="font-bold flex items-center gap-1">
                    <AlertCircle className="size-3.5" /> Error Encountered
                  </span>
                  <p className="text-[11px] font-mono">{node.error}</p>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}
