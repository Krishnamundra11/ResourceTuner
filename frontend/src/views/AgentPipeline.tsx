/** Agent Pipeline — the run as a graph of stages, with the evidence each one recorded.
 *
 *  Status comes only from events the backend emitted. A stage with no event is "waiting", and a
 *  historical record that never persisted a stage is "Evidence unavailable" — neither is ever
 *  filled in with a guess. */

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { PayloadView } from "@/components/PayloadView";
import { PipelineGraph, PipelineLegend } from "@/components/PipelineGraph";
import { KeyValue, PageHeader, Panel, SectionTitle } from "@/components/primitives";
import { RunPicker, useRunSelection } from "@/components/RunPicker";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "@/components/StateViews";
import { Badge } from "@/components/ui/badge";
import { latestStageStatus } from "@/hooks/useEventStream";
import { clockTime, ms, titleCase } from "@/lib/format";
import {
  PIPELINE_ORDER, RUN_LEVEL_STAGES, STAGE_LABELS, STAGE_STATUS_META, STAGE_SUBTITLES,
} from "@/lib/status";

export default function AgentPipeline() {
  const sel = useRunSelection();
  const [stage, setStage] = useState<string | null>(null);

  const statuses = useMemo(() => latestStageStatus(sel.events, sel.cycle), [sel.events, sel.cycle]);

  const runLevel = useMemo(
    () => (RUN_LEVEL_STAGES as readonly string[])
      .map((s) => [...sel.events].reverse().find((e) => e.stage === s))
      .filter((e): e is NonNullable<typeof e> => !!e),
    [sel.events],
  );

  const selectedEvent = stage ? statuses.get(stage) ?? null : null;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Agent Pipeline"
        subtitle="One cycle, stage by stage. Deterministic components do the measuring, classifying, gating and evaluating; the ADK Supervisor, the CrewAI diagnostic crew, the Decision Agent and the Meta-Agent do the reasoning. The Supervisor calls the whole crew as a single step."
        actions={<RunPicker sel={sel} />}
      />

      {sel.error ? <ErrorPanel error={sel.error} /> : null}

      {!sel.run ? (
        <EmptyPanel title="No runs recorded yet"
                    hint={<>Start one from <Link className="text-accent hover:underline" to="/live-lab">Live Lab</Link>.</>} />
      ) : sel.isPending ? (
        <LoadingPanel rows={6} />
      ) : (
        <>
          {runLevel.length > 0 && (
            <section>
              <SectionTitle>Run-level stages</SectionTitle>
              <div className="grid gap-2 sm:grid-cols-3">
                {runLevel.map((e) => {
                  const meta = STAGE_STATUS_META[e.status];
                  return (
                    <div key={e.stage} className="rounded-lg border bg-panel px-3 py-2"
                         style={{ borderColor: meta.color }}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-xs font-medium text-fg">{STAGE_LABELS[e.stage] ?? e.stage}</span>
                        <span className="size-1.5 rounded-full" style={{ background: meta.color }} />
                      </div>
                      <p className="mt-1 text-[11px] leading-snug text-fg-muted">{e.summary || meta.label}</p>
                    </div>
                  );
                })}
              </div>
            </section>
          )}

          <section>
            <SectionTitle right={
              sel.cycle !== null
                ? <span className="text-[11px] text-fg-subtle">Cycle {sel.cycle}</span>
                : <span className="text-[11px] text-fg-subtle">no cycle stages yet</span>
            }>
              Cycle pipeline
            </SectionTitle>
            <PipelineGraph statuses={statuses} onSelect={setStage} />
            <div className="mt-2"><PipelineLegend /></div>
            <p className="mt-1.5 text-[11px] text-fg-subtle">Click a stage to read what it recorded.</p>
          </section>

          <section className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
            <Panel title="Stage detail">
              {!stage ? (
                <EmptyPanel title="No stage selected" hint="Pick a box in the graph above." />
              ) : !selectedEvent ? (
                <div className="space-y-2">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-fg">{STAGE_LABELS[stage] ?? titleCase(stage)}</span>
                    <Badge tone="neutral">Waiting</Badge>
                  </div>
                  <p className="text-[11px] text-fg-subtle">
                    This stage has not reported in {sel.cycle === null ? "this run" : `cycle ${sel.cycle}`}.
                    Nothing is shown for it because nothing was recorded.
                  </p>
                </div>
              ) : (
                <div className="space-y-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-fg">
                      {STAGE_LABELS[selectedEvent.stage] ?? titleCase(selectedEvent.stage)}
                    </span>
                    <Badge tone={STAGE_STATUS_META[selectedEvent.status].tone}>
                      {STAGE_STATUS_META[selectedEvent.status].label}
                    </Badge>
                    {STAGE_SUBTITLES[selectedEvent.stage] && (
                      <span className="text-[11px] text-fg-subtle">{STAGE_SUBTITLES[selectedEvent.stage]}</span>
                    )}
                  </div>
                  <p className="text-xs leading-relaxed text-fg-muted">{selectedEvent.summary || "—"}</p>
                  <KeyValue rows={[
                    { k: "Recorded at", v: clockTime(selectedEvent.ts) },
                    { k: "Duration", v: ms(selectedEvent.duration_ms) },
                    { k: "Event seq", v: selectedEvent.seq },
                    { k: "Cycle", v: selectedEvent.cycle ?? "run-level" },
                  ]} />
                  <PayloadView value={selectedEvent.payload} defaultOpen />
                </div>
              )}
            </Panel>

            <Panel title="Stage timeline" hint="Every event for this cycle in the order the backend wrote them."
                   bodyClassName="p-0">
              <StageTimeline statuses={statuses} onSelect={setStage} selected={stage} />
            </Panel>
          </section>
        </>
      )}
    </div>
  );
}

function StageTimeline({ statuses, onSelect, selected }: {
  statuses: Map<string, ReturnType<typeof latestStageStatus> extends Map<string, infer V> ? V : never>;
  onSelect: (s: string) => void;
  selected: string | null;
}) {
  return (
    <ul className="divide-y divide-border/60">
      {PIPELINE_ORDER.map((s) => {
        const e = statuses.get(s);
        const meta = STAGE_STATUS_META[e?.status ?? "waiting"];
        return (
          <li key={s}>
            <button type="button" onClick={() => onSelect(s)}
                    className={`flex w-full items-baseline gap-2 px-3 py-1.5 text-left text-[11px] hover:bg-panel-hover ${
                      selected === s ? "bg-panel-hover" : ""}`}>
              <span className="mt-1 size-1.5 shrink-0 rounded-full" style={{ background: meta.color }} />
              <span className="w-36 shrink-0 text-fg-muted">{STAGE_LABELS[s] ?? s}</span>
              <span className="min-w-0 flex-1 truncate text-fg">{e?.summary || meta.label}</span>
              <span className="nums shrink-0 text-fg-subtle">{e ? ms(e.duration_ms) : ""}</span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
