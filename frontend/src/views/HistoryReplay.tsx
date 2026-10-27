/** Historical Runs & Replay — strictly read-only.
 *
 *  Replay steps through records that were written when the cycle ran. Selecting a run, pressing
 *  Play or dragging the scrubber reads from the event store or from the legacy cycle log and
 *  nothing else: no agent is rerun, no LLM is called, no container is touched, no cgroup is
 *  written and no policy is changed. That is structural, not a promise — the endpoints behind
 *  this view are served by dashboard/history.py, which imports nothing that can act.
 *
 *  Two sources, kept apart because they carry different amounts of evidence:
 *
 *  * **Recorded runs** — written by the event layer, one event per stage, with timings.
 *  * **Legacy cycles** — one summary row per cycle, written before the event layer existed.
 *    Stages whose details were never persisted are shown as "Evidence unavailable". They are not
 *    reconstructed, and they are not shown as completed.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronLeft, ChevronRight, Lock, Pause, Play, RotateCcw,
} from "lucide-react";

import { PayloadView } from "@/components/PayloadView";
import { PipelineGraph, PipelineLegend } from "@/components/PipelineGraph";
import { KeyValue, PageHeader, Panel, ReadOnlyNote, SectionTitle } from "@/components/primitives";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "@/components/StateViews";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Slider, Table, Td, Th } from "@/components/ui/misc";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useRunEventsAll, useRunList } from "@/hooks/useRuns";
import { useHistoryCycle, useHistoryCycles } from "@/hooks/useHistory";
import { DASH, clockTime, dateTime, ms, seconds, signedPct, titleCase } from "@/lib/format";
import {
  PIPELINE_ORDER, RUN_STATUS_META, STAGE_LABELS, STAGE_STATUS_META, verdictTone,
} from "@/lib/status";
import type { StageEvent, StageStatus } from "@/lib/types";

/** One replay frame: a stage as some record described it. */
interface Frame {
  stage: string;
  status: StageStatus;
  summary: string;
  payload: Record<string, unknown>;
  ts: number | null;
  durationMs: number | null;
  /** True for a stage the record never persisted — shown, never filled in. */
  unavailable: boolean;
}

const SPEEDS = [0.5, 1, 2, 4] as const;

export default function HistoryReplay() {
  return (
    <div className="space-y-4">
      <PageHeader
        title="Historical Runs & Replay"
        subtitle="Step through what was recorded. This view can only read: selecting a run or pressing Play does not rerun an agent, call a model, change a container or modify a policy."
        actions={<Badge tone="neutral"><Lock className="mr-1 inline size-3" />read-only</Badge>}
      />

      <Tabs defaultValue="runs">
        <TabsList>
          <TabsTrigger value="runs">Recorded runs</TabsTrigger>
          <TabsTrigger value="legacy">Legacy cycles</TabsTrigger>
        </TabsList>
        <TabsContent value="runs" className="mt-3"><RecordedRuns /></TabsContent>
        <TabsContent value="legacy" className="mt-3"><LegacyCycles /></TabsContent>
      </Tabs>

      <ReadOnlyNote>
        The endpoints behind this page (<span className="font-mono">/api/history/*</span> and the
        run event store) are served by a module that imports no runner, no controller and no agent.
        A replay therefore cannot act even by mistake — there is no code path from here to a
        container or a cgroup.
      </ReadOnlyNote>
    </div>
  );
}

// --- source 1: runs written by the event layer ---------------------------------------------

function RecordedRuns() {
  const list = useRunList({ limit: 100 });
  const [runId, setRunId] = useState<string | null>(null);
  const [cycle, setCycle] = useState<number | null>(null);

  const runs = list.data?.runs ?? [];
  const finished = runs.filter((r) => !r.active);
  const selectedId = runId ?? finished[0]?.run_id ?? null;
  const run = runs.find((r) => r.run_id === selectedId) ?? null;

  const events = useRunEventsAll(selectedId, !!selectedId);
  const all = useMemo(() => events.data ?? [], [events.data]);

  const cycles = useMemo(
    () => [...new Set(all.map((e) => e.cycle).filter((c): c is number => c !== null))].sort((a, b) => a - b),
    [all],
  );
  const activeCycle = cycle !== null && cycles.includes(cycle) ? cycle : cycles[0] ?? null;

  const frames = useMemo<Frame[]>(() => framesFromEvents(all, activeCycle), [all, activeCycle]);

  if (list.isPending) return <LoadingPanel rows={5} />;
  if (list.error) return <ErrorPanel error={list.error} onRetry={() => void list.refetch()} />;
  if (!runs.length) {
    return (
      <Panel>
        <EmptyPanel title="No runs recorded yet"
                    hint="A run appears here as soon as one has been started — from Live Lab or from the CLI." />
      </Panel>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Select value={selectedId ?? ""} onValueChange={(v) => { setRunId(v); setCycle(null); }}>
          <SelectTrigger className="w-[340px]"><SelectValue placeholder="Pick a run" /></SelectTrigger>
          <SelectContent>
            {runs.map((r) => (
              <SelectItem key={r.run_id} value={r.run_id}>
                {r.run_id} · {r.scenario ?? DASH} · {RUN_STATUS_META[r.status].label} · {dateTime(r.created_at)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {cycles.length > 0 && (
          <Select value={activeCycle === null ? "" : String(activeCycle)}
                  onValueChange={(v) => setCycle(Number(v))}>
            <SelectTrigger className="w-[140px]"><SelectValue placeholder="Cycle" /></SelectTrigger>
            <SelectContent>
              {cycles.map((c) => <SelectItem key={c} value={String(c)}>Cycle {c}</SelectItem>)}
            </SelectContent>
          </Select>
        )}
        {run && <Badge tone={RUN_STATUS_META[run.status].tone}>{RUN_STATUS_META[run.status].label}</Badge>}
        {run?.active && <span className="text-[11px] text-warn">This run is still going — replay shows what has been written so far.</span>}
      </div>

      {run && (
        <Panel>
          <KeyValue columns={2} rows={[
            { k: "Run id", v: run.run_id },
            { k: "Started by", v: run.kind === "cli" ? "command line" : "dashboard" },
            { k: "Scenario", v: `${run.scenario ?? DASH}${run.level ? ` · ${run.level}` : ""}` },
            { k: "Cycles", v: `${run.cycles_done} / ${run.cycles_requested}` },
            { k: "Created", v: dateTime(run.created_at) },
            { k: "Ended", v: run.ended_at ? dateTime(run.ended_at) : "—" },
          ]} />
          {run.error && <p className="mt-2 text-[11px] text-bad">{run.error}</p>}
        </Panel>
      )}

      {events.isPending ? <LoadingPanel rows={5} />
        : events.error ? <ErrorPanel error={events.error} />
          : frames.length === 0
            ? <Panel><EmptyPanel title="This run recorded no cycle stages" /></Panel>
            : <Player frames={frames} />}
    </div>
  );
}

// --- source 2: the pre-event-layer cycle log -------------------------------------------------

function LegacyCycles() {
  const cycles = useHistoryCycles({ limit: 100 });
  const [picked, setPicked] = useState<number | null>(null);
  const rows = cycles.data?.cycles ?? [];
  const activeCycle = picked ?? rows[0]?.cycle ?? null;
  const detail = useHistoryCycle(activeCycle);

  const frames = useMemo<Frame[]>(() => {
    const stages = detail.data?.stages ?? [];
    return stages.map((s) => ({
      stage: s.stage,
      status: s.status,
      summary: s.summary,
      payload: s.payload,
      ts: s.ts,
      durationMs: s.duration_ms,
      unavailable: s.status === "evidence_unavailable",
    }));
  }, [detail.data]);

  if (cycles.isPending) return <LoadingPanel rows={5} />;
  if (cycles.error) return <ErrorPanel error={cycles.error} onRetry={() => void cycles.refetch()} />;
  if (!rows.length) {
    return (
      <Panel>
        <EmptyPanel title="No legacy cycle records"
                    hint="data/vertical_slice_log.jsonl is empty or absent." />
      </Panel>
    );
  }

  const unavailableCount = frames.filter((f) => f.unavailable).length;

  return (
    <div className="space-y-3">
      <p className="rounded-md border border-unknown/40 bg-bg-raised px-3 py-2 text-[11px] leading-relaxed text-fg-muted">
        {cycles.data?.note} Where a stage's details were never written, the replay says
        <strong className="text-fg"> Evidence unavailable</strong> rather than guessing at them.
      </p>

      <Panel bodyClassName="p-0">
        <Table>
          <thead>
            <tr>
              <Th>Cycle</Th><Th>When</Th><Th>Trigger</Th><Th>Action</Th><Th>Gate</Th>
              <Th>Verdict</Th><Th className="text-right">Delta</Th>
              <Th className="text-right">Before</Th><Th className="text-right">After</Th><Th />
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.cycle}
                  className={`border-t border-border/60 ${c.cycle === activeCycle ? "bg-panel-hover" : ""}`}>
                <Td className="nums font-medium text-fg">{c.cycle}</Td>
                <Td className="text-fg-subtle">{dateTime(c.timestamp)}</Td>
                <Td className="nums text-fg-muted">
                  {c.trigger ? `${c.trigger.metric ?? c.trigger.resource ?? ""} ${c.trigger.value ?? ""}` : DASH}
                </Td>
                <Td className="text-fg">{c.action ? c.action.tool : <span className="text-fg-subtle">no action</span>}</Td>
                <Td className="text-fg-muted">{c.execution_status ?? DASH}</Td>
                <Td>{c.verdict ? <Badge tone={verdictTone(c.verdict)}>{c.verdict}</Badge> : <span className="text-fg-subtle">{DASH}</span>}</Td>
                <Td className="nums text-right">{c.improvement_pct === null ? DASH : signedPct(c.improvement_pct, 2)}</Td>
                <Td className="nums text-right">{c.before_s === null ? DASH : seconds(c.before_s)}</Td>
                <Td className="nums text-right">{c.after_s === null ? DASH : seconds(c.after_s)}</Td>
                <Td>
                  <Button size="sm" variant={c.cycle === activeCycle ? "secondary" : "ghost"}
                          onClick={() => setPicked(c.cycle)}>
                    Replay
                  </Button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Panel>

      {detail.isPending ? <LoadingPanel rows={5} />
        : detail.error ? <ErrorPanel error={detail.error} />
          : frames.length === 0
            ? <Panel><EmptyPanel title="Pick a cycle to replay" /></Panel>
            : (
              <>
                {unavailableCount > 0 && (
                  <p className="text-[11px] text-fg-subtle">
                    {unavailableCount} of {frames.length} stages in this cycle have no persisted evidence.
                  </p>
                )}
                <Player frames={frames} />
              </>
            )}
    </div>
  );
}

// --- the player -----------------------------------------------------------------------------

/** Frames for one cycle, in pipeline order. A stage with no event is a waiting frame. */
function framesFromEvents(events: StageEvent[], cycle: number | null): Frame[] {
  if (cycle === null) return [];
  const latest = new Map<string, StageEvent>();
  for (const e of events) {
    if (e.cycle !== cycle) continue;
    const prev = latest.get(e.stage);
    if (!prev || e.seq > prev.seq) latest.set(e.stage, e);
  }
  if (latest.size === 0) return [];
  return PIPELINE_ORDER.map((stage) => {
    const e = latest.get(stage);
    return {
      stage,
      status: e?.status ?? "waiting",
      summary: e?.summary ?? "",
      payload: e?.payload ?? {},
      ts: e?.ts ?? null,
      durationMs: e?.duration_ms ?? null,
      unavailable: !e,
    };
  });
}

const STEP_MS = 1100;

function Player({ frames }: { frames: Frame[] }) {
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<number>(1);
  const timer = useRef<number | null>(null);

  // Restart the walkthrough whenever a different record is loaded.
  useEffect(() => { setIndex(0); setPlaying(false); }, [frames]);

  useEffect(() => {
    if (!playing) return;
    timer.current = window.setInterval(() => {
      setIndex((i) => {
        if (i >= frames.length - 1) { setPlaying(false); return i; }
        return i + 1;
      });
    }, STEP_MS / speed);
    return () => { if (timer.current !== null) window.clearInterval(timer.current); };
  }, [playing, speed, frames.length]);

  const atEnd = index >= frames.length - 1;
  const current = frames[index];

  // Only the stages up to the cursor are revealed, so the graph fills in as the replay walks.
  const statuses = useMemo(() => {
    const map = new Map<string, StageEvent>();
    frames.slice(0, index + 1).forEach((f, i) => {
      map.set(f.stage, {
        seq: i, run_id: "replay", cycle: null, stage: f.stage, status: f.status,
        ts: f.ts ?? 0, duration_ms: f.durationMs, summary: f.summary, payload: f.payload,
      });
    });
    return map;
  }, [frames, index]);

  return (
    <div className="space-y-3">
      <Panel
        title="Replay"
        right={
          <div className="flex items-center gap-1.5">
            <Button size="sm" variant="ghost" aria-label="Restart"
                    onClick={() => { setIndex(0); setPlaying(false); }}>
              <RotateCcw className="size-3.5" />
            </Button>
            <Button size="sm" variant="ghost" aria-label="Previous stage"
                    disabled={index === 0}
                    onClick={() => { setPlaying(false); setIndex((i) => Math.max(0, i - 1)); }}>
              <ChevronLeft className="size-3.5" />
            </Button>
            <Button size="sm" variant={playing ? "secondary" : "default"}
                    aria-label={playing ? "Pause" : "Play"}
                    onClick={() => {
                      if (atEnd && !playing) setIndex(0);
                      setPlaying((p) => !p);
                    }}>
              {playing ? <Pause className="size-3.5" /> : <Play className="size-3.5" />}
              {playing ? "Pause" : "Play"}
            </Button>
            <Button size="sm" variant="ghost" aria-label="Next stage"
                    disabled={atEnd}
                    onClick={() => { setPlaying(false); setIndex((i) => Math.min(frames.length - 1, i + 1)); }}>
              <ChevronRight className="size-3.5" />
            </Button>
            <Select value={String(speed)} onValueChange={(v) => setSpeed(Number(v))}>
              <SelectTrigger className="h-8 w-[84px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                {SPEEDS.map((s) => <SelectItem key={s} value={String(s)}>{s}×</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
        }
      >
        <div className="space-y-3">
          <div className="flex items-center gap-3">
            <span className="nums w-20 shrink-0 text-[11px] text-fg-subtle">
              {index + 1} / {frames.length}
            </span>
            <Slider min={0} max={Math.max(0, frames.length - 1)} step={1} value={[index]}
                    aria-label="Replay position"
                    onValueChange={([v]) => { setPlaying(false); setIndex(v); }} />
          </div>

          <ol className="flex flex-wrap gap-1">
            {frames.map((f, i) => (
              <li key={f.stage}>
                <button type="button"
                        onClick={() => { setPlaying(false); setIndex(i); }}
                        className={`flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] transition-colors ${
                          i === index ? "border-accent text-fg" : "border-border text-fg-subtle hover:text-fg-muted"}`}>
                  <span className="size-1.5 rounded-full"
                        style={{ background: i <= index ? STAGE_STATUS_META[f.status].color : "var(--color-border-strong)" }} />
                  {STAGE_LABELS[f.stage] ?? titleCase(f.stage)}
                </button>
              </li>
            ))}
          </ol>
        </div>
      </Panel>

      <PipelineGraph statuses={statuses} height={360} />
      <PipelineLegend />

      <Panel title={STAGE_LABELS[current.stage] ?? titleCase(current.stage)}
             right={<Badge tone={STAGE_STATUS_META[current.status].tone}>
               {STAGE_STATUS_META[current.status].label}
             </Badge>}>
        {current.unavailable ? (
          <div className="space-y-2">
            <p className="text-xs leading-relaxed text-fg-muted">
              <strong className="text-unknown">Evidence unavailable.</strong>{" "}
              {current.summary || "This stage's details were not persisted when the cycle ran."}
            </p>
            <p className="text-[11px] leading-relaxed text-fg-subtle">
              The record this replay reads has no event for this stage, so there is nothing to
              show. It is not reported as completed, skipped or failed, because the record does not
              say which it was.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            <p className="text-xs leading-relaxed text-fg-muted">{current.summary || DASH}</p>
            <SectionTitle>Recorded</SectionTitle>
            <KeyValue rows={[
              { k: "Timestamp", v: current.ts ? clockTime(current.ts) : "not recorded" },
              { k: "Duration", v: current.durationMs === null ? "not recorded" : ms(current.durationMs) },
            ]} />
            <PayloadView value={current.payload} label="Stage payload" defaultOpen />
          </div>
        )}
      </Panel>
    </div>
  );
}
