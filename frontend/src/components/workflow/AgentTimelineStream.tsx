/** Execution Timeline & Data Movement Stream for the Agent Workflow visualization.
 *
 *  Displays chronological events with:
 *  - Exact timestamps
 *  - Event type badges (Start, Input, Processing, Handoff, Check, Error)
 *  - Source -> Target handoffs
 *  - Click to inspect node
 */

import { useState } from "react";
import { Clock } from "lucide-react";
import type { WorkflowTimelineEvent } from "./workflowTypes";
import { STAGE_STATUS_META } from "@/lib/status";
import { cn } from "@/lib/utils";

interface AgentTimelineStreamProps {
  events: WorkflowTimelineEvent[];
  onSelectNode: (nodeId: string) => void;
}

export function AgentTimelineStream({ events, onSelectNode }: AgentTimelineStreamProps) {
  const [filter, setFilter] = useState<"all" | "handoff" | "agent" | "check">("all");
  const [search, setSearch] = useState("");

  const filtered = events.filter((ev) => {
    if (filter === "handoff" && ev.type !== "handoff") return false;
    if (filter === "check" && ev.type !== "check") return false;
    if (filter === "agent" && !["supervisor", "diagnostics", "decision", "meta"].some((s) => ev.sourceNodeId.toLowerCase().includes(s))) return false;
    if (search && !ev.title.toLowerCase().includes(search.toLowerCase()) && !ev.description.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  });

  return (
    <div className="rounded-xl border border-border bg-panel shadow-sm">
      {/* Header with Filters */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border p-3.5">
        <div className="flex items-center gap-2">
          <Clock className="size-4 text-accent" />
          <h4 className="text-xs font-semibold text-fg">Agent Execution Timeline & Data Stream</h4>
          <span className="rounded-full bg-bg-raised px-2 py-0.5 text-[10px] font-mono text-fg-subtle border border-border">
            {events.length} events
          </span>
        </div>

        <div className="flex items-center gap-2 text-xs">
          <div className="flex rounded-lg border border-border bg-bg-raised p-0.5">
            {[
              { id: "all", label: "All" },
              { id: "agent", label: "Agents" },
              { id: "handoff", label: "Handoffs" },
              { id: "check", label: "Safety" },
            ].map((f) => (
              <button
                key={f.id}
                onClick={() => setFilter(f.id as typeof filter)}
                className={cn(
                  "rounded px-2.5 py-1 text-[11px] font-medium transition-colors",
                  filter === f.id ? "bg-panel text-fg font-semibold shadow-xs" : "text-fg-subtle hover:text-fg",
                )}
              >
                {f.label}
              </button>
            ))}
          </div>

          <div className="relative">
            <input
              type="text"
              placeholder="Search timeline..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-7 w-36 rounded-md border border-border bg-bg px-2 text-[11px] text-fg placeholder:text-fg-subtle focus:w-48 focus:border-accent focus:outline-none transition-all"
            />
          </div>
        </div>
      </div>

      {/* Events List */}
      <div className="divide-y divide-border/40 max-h-64 overflow-y-auto font-sans text-xs">
        {filtered.length === 0 ? (
          <div className="p-6 text-center text-xs text-fg-subtle italic">
            No execution events match the current filter.
          </div>
        ) : (
          filtered.map((ev) => {
            const meta = STAGE_STATUS_META[ev.status] || STAGE_STATUS_META.waiting;
            return (
              <div
                key={ev.id}
                onClick={() => onSelectNode(ev.sourceNodeId)}
                className="flex items-center justify-between gap-3 p-2.5 hover:bg-panel-hover/50 cursor-pointer transition-colors"
              >
                <div className="flex items-center gap-2.5 min-w-0">
                  <span className="font-mono text-[10px] text-fg-subtle shrink-0">
                    {ev.timeFormatted}
                  </span>

                  <span
                    className="size-2 rounded-full shrink-0"
                    style={{ background: meta.color }}
                  />

                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="font-semibold text-fg text-xs truncate">
                        {ev.title}
                      </span>
                      <span className="rounded bg-bg px-1.5 py-0.5 text-[9px] font-mono text-fg-subtle border border-border uppercase">
                        {ev.type}
                      </span>
                    </div>
                    <p className="text-[11px] text-fg-muted truncate">{ev.description}</p>
                  </div>
                </div>

                <div className="flex items-center gap-2 shrink-0">
                  {ev.payloadSnippet && (
                    <span className="font-mono text-[10px] text-fg-subtle max-w-[140px] truncate hidden md:inline">
                      {ev.payloadSnippet}
                    </span>
                  )}
                  <span className="text-[10px] text-accent hover:underline font-medium">Inspect →</span>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
