/** The pipeline as a graph, coloured by what each stage actually reported.
 *
 *  Layout is fixed: the architecture does not change between runs, only the status of each box.
 *  A stage with no event is "waiting" — never guessed, never shown as completed.
 *
 *  The node styling carries the §2/§4 split the project is built on: deterministic components
 *  are visually plain, and the two agent frameworks are labelled separately, because the ADK
 *  Supervisor calls the whole CrewAI crew as a single step and does not know what runs inside it.
 */

import { useMemo } from "react";
import {
  Background, BackgroundVariant, Controls, Handle, Position, ReactFlow,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { Badge } from "./ui/badge";
import { ms } from "@/lib/format";
import { PIPELINE_ORDER, STAGE_LABELS, STAGE_STATUS_META, STAGE_SUBTITLES } from "@/lib/status";
import type { StageEvent, StageStatus } from "@/lib/types";

type Kind = "deterministic" | "adk" | "crewai";

const KIND: Record<string, Kind> = {
  trigger: "deterministic", classify: "deterministic", safety_gate: "deterministic",
  controller: "deterministic", post_measure: "deterministic", evaluation: "deterministic",
  policy: "deterministic", supervisor: "adk", decision: "adk", meta: "adk",
  diagnostics: "crewai",
};

const KIND_LABEL: Record<Kind, string> = {
  deterministic: "Deterministic", adk: "Google ADK", crewai: "CrewAI",
};

/** Snake layout: 4 per row, so 11 stages fit without a scrollbar on a laptop. */
const COLS = 4;
const W = 196;
const H = 86;
const GAP_X = 52;
const GAP_Y = 46;

function position(i: number) {
  const row = Math.floor(i / COLS);
  const col = row % 2 === 0 ? i % COLS : COLS - 1 - (i % COLS);
  return { x: col * (W + GAP_X), y: row * (H + GAP_Y) };
}

export interface StageNodeData extends Record<string, unknown> {
  stage: string;
  kind: Kind;
  status: StageStatus;
  summary: string;
  durationMs: number | null;
  /** The crew's three members, shown inside the single diagnostics step. */
  members?: string[];
  selected?: boolean;
}

function StageNode({ data }: NodeProps<Node<StageNodeData>>) {
  const meta = STAGE_STATUS_META[data.status];
  const running = data.status === "running";
  return (
    <div
      className="rounded-lg border bg-panel px-2.5 py-2 text-left shadow-sm transition-colors"
      style={{
        width: W, height: H,
        borderColor: data.status === "waiting" ? "var(--color-border)" : meta.color,
        boxShadow: running ? `0 0 0 3px color-mix(in oklab, ${meta.color} 22%, transparent)` : undefined,
      }}
    >
      <Handle type="target" position={Position.Left} className="!size-1.5 !border-0 !bg-border-strong" />
      <div className="flex items-start justify-between gap-1">
        <span className="text-[11px] font-semibold leading-tight text-fg">
          {STAGE_LABELS[data.stage] ?? data.stage}
        </span>
        <span className={running ? "size-1.5 shrink-0 animate-pulse rounded-full" : "size-1.5 shrink-0 rounded-full"}
              style={{ background: meta.color }} aria-label={meta.label} />
      </div>
      <div className="mt-0.5 text-[9px] uppercase tracking-wide text-fg-subtle">
        {KIND_LABEL[data.kind]}
        {STAGE_SUBTITLES[data.stage] ? ` · ${STAGE_SUBTITLES[data.stage]}` : ""}
      </div>
      {data.members ? (
        <div className="mt-1 flex gap-1">
          {data.members.map((m) => (
            <span key={m} className="rounded border border-border px-1 text-[9px] text-fg-muted">{m}</span>
          ))}
        </div>
      ) : (
        <div className="mt-1 line-clamp-2 text-[10px] leading-tight text-fg-muted">
          {data.summary || meta.label}
        </div>
      )}
      {data.durationMs !== null && (
        <div className="nums absolute bottom-1.5 right-2.5 text-[9px] text-fg-subtle">{ms(data.durationMs)}</div>
      )}
      <Handle type="source" position={Position.Right} className="!size-1.5 !border-0 !bg-border-strong" />
    </div>
  );
}

const nodeTypes = { stage: StageNode };

export function PipelineGraph({ statuses, onSelect, height = 420 }: {
  /** Latest event per stage id; a missing stage renders as waiting. */
  statuses: Map<string, StageEvent>;
  onSelect?: (stage: string) => void;
  height?: number;
}) {
  const nodes = useMemo<Node<StageNodeData>[]>(
    () => PIPELINE_ORDER.map((stage, i) => {
      const e = statuses.get(stage);
      return {
        id: stage,
        type: "stage",
        position: position(i),
        draggable: false,
        data: {
          stage,
          kind: KIND[stage] ?? "deterministic",
          status: e?.status ?? "waiting",
          summary: e?.summary ?? "",
          durationMs: e?.duration_ms ?? null,
          members: stage === "diagnostics" ? ["CPU", "RAM", "I/O"] : undefined,
        },
      };
    }),
    [statuses],
  );

  const edges = useMemo<Edge[]>(
    () => PIPELINE_ORDER.slice(0, -1).map((stage, i) => {
      const next = PIPELINE_ORDER[i + 1];
      const from = statuses.get(stage);
      const to = statuses.get(next);
      const live = to?.status === "running";
      return {
        id: `${stage}->${next}`,
        source: stage,
        target: next,
        type: "smoothstep",
        animated: live,
        style: {
          stroke: from && from.status !== "waiting" ? "var(--color-border-strong)" : "var(--color-border)",
          strokeWidth: 1.5,
        },
      };
    }),
    [statuses],
  );

  return (
    <div style={{ height }} className="rounded-lg border border-border bg-bg-raised">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.12 }}
        minZoom={0.3}
        maxZoom={1.4}
        proOptions={{ hideAttribution: true }}
        nodesConnectable={false}
        onNodeClick={(_, n) => onSelect?.(n.id)}
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--color-border)" />
        <Controls showInteractive={false} position="bottom-right" />
      </ReactFlow>
    </div>
  );
}

/** Legend for the graph: statuses and the framework split, so colour is never the only cue. */
export function PipelineLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] text-fg-subtle">
      {(Object.keys(STAGE_STATUS_META) as StageStatus[]).map((s) => (
        <span key={s} className="flex items-center gap-1.5">
          <span className="size-2 rounded-full" style={{ background: STAGE_STATUS_META[s].color }} />
          {STAGE_STATUS_META[s].label}
        </span>
      ))}
      <span className="ml-auto flex items-center gap-2">
        <Badge tone="neutral">Deterministic</Badge>
        <Badge tone="accent">Google ADK</Badge>
        <Badge tone="accent">CrewAI</Badge>
      </span>
    </div>
  );
}
