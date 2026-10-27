/** Run + cycle selector, and the one place that decides where a run's events come from:
 *  a live run is followed over SSE, a finished one is read back from the event store. */

import { useEffect, useMemo, useState } from "react";

import { Badge } from "./ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "./ui/select";
import { useEventStream } from "@/hooks/useEventStream";
import { useActiveRun, useRunEventsAll, useRunList } from "@/hooks/useRuns";
import { dateTime } from "@/lib/format";
import { RUN_STATUS_META } from "@/lib/status";
import type { Run, StageEvent } from "@/lib/types";

export interface RunSelection {
  runs: Run[];
  run: Run | null;
  setRunId: (id: string) => void;
  events: StageEvent[];
  /** Cycle numbers the events actually contain, ascending. */
  cycles: number[];
  cycle: number | null;
  setCycle: (c: number) => void;
  live: boolean;
  isPending: boolean;
  error: unknown;
}

export function useRunSelection(): RunSelection {
  const active = useActiveRun();
  const list = useRunList({ limit: 50 });
  const [runId, setRunId] = useState<string | null>(null);
  const [cycle, setCycle] = useState<number | null>(null);

  const runs = useMemo(() => list.data?.runs ?? [], [list.data]);
  const activeRun = active.data?.active ?? null;

  // Follow the active run unless the user has picked another one.
  useEffect(() => {
    if (runId === null && (activeRun || runs.length)) setRunId(activeRun?.run_id ?? runs[0].run_id);
  }, [runId, activeRun, runs]);

  const run = runs.find((r) => r.run_id === runId) ?? activeRun ?? null;
  const live = !!run?.active;

  const stream = useEventStream({ runId: run?.run_id, enabled: live });
  const stored = useRunEventsAll(run?.run_id ?? null, !live);
  const events = useMemo(
    () => (live ? stream.events : stored.data ?? []),
    [live, stream.events, stored.data],
  );

  const cycles = useMemo(
    () => [...new Set(events.map((e) => e.cycle).filter((c): c is number => c !== null))].sort((a, b) => a - b),
    [events],
  );

  // Default to the newest cycle, and follow it forward while a run is live.
  useEffect(() => {
    if (!cycles.length) { if (cycle !== null) setCycle(null); return; }
    if (cycle === null || !cycles.includes(cycle) || (live && cycle < cycles[cycles.length - 1])) {
      setCycle(cycles[cycles.length - 1]);
    }
  }, [cycles, cycle, live]);

  return {
    runs: activeRun && !runs.some((r) => r.run_id === activeRun.run_id) ? [activeRun, ...runs] : runs,
    run,
    setRunId: (id) => { setRunId(id); setCycle(null); },
    events,
    cycles,
    cycle,
    setCycle,
    live,
    isPending: list.isPending || (!live && stored.isPending && !!run),
    error: list.error ?? (live ? null : stored.error),
  };
}

export function RunPicker({ sel }: { sel: RunSelection }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select value={sel.run?.run_id ?? ""} onValueChange={sel.setRunId}>
        <SelectTrigger className="w-[320px]"><SelectValue placeholder="Pick a run" /></SelectTrigger>
        <SelectContent>
          {sel.runs.map((r) => (
            <SelectItem key={r.run_id} value={r.run_id}>
              {r.run_id} · {r.scenario ?? "—"} · {RUN_STATUS_META[r.status].label} · {dateTime(r.created_at)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {sel.cycles.length > 0 && (
        <Select value={sel.cycle === null ? "" : String(sel.cycle)}
                onValueChange={(v) => sel.setCycle(Number(v))}>
          <SelectTrigger className="w-[140px]"><SelectValue placeholder="Cycle" /></SelectTrigger>
          <SelectContent>
            {sel.cycles.map((c) => <SelectItem key={c} value={String(c)}>Cycle {c}</SelectItem>)}
          </SelectContent>
        </Select>
      )}

      {sel.run && (
        <Badge tone={RUN_STATUS_META[sel.run.status].tone}>
          {sel.live ? "live" : RUN_STATUS_META[sel.run.status].label}
        </Badge>
      )}
    </div>
  );
}
