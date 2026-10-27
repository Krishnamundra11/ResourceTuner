import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ApiError, type StartRunBody } from "@/lib/api";
import type { Run, RunList, ScenarioList, StageEvent, StopResult } from "@/lib/types";

export function useScenarios() {
  return useQuery<ScenarioList, ApiError>({
    queryKey: ["scenarios"],
    queryFn: api.scenarios,
    staleTime: 5 * 60_000,            // the scenario files do not change while the app runs
  });
}

/** The one run that may be in flight. Polled briskly so Start/Stop feel immediate. */
export function useActiveRun(intervalMs = 2000) {
  return useQuery<{ active: Run | null }, ApiError>({
    queryKey: ["runs", "active"],
    queryFn: api.activeRun,
    refetchInterval: intervalMs,
    retry: false,
  });
}

export function useRunList(params: { limit?: number; offset?: number; status?: string } = {}) {
  return useQuery<RunList, ApiError>({
    queryKey: ["runs", "list", params],
    queryFn: () => api.runs(params),
    refetchInterval: 5000,
  });
}

export function useRun(runId: string | null, intervalMs = 2000) {
  return useQuery<Run, ApiError>({
    queryKey: ["runs", runId],
    queryFn: () => api.run(runId!),
    enabled: !!runId,
    refetchInterval: (q) => (q.state.data?.active ? intervalMs : false),
  });
}

export function useStartRun() {
  const qc = useQueryClient();
  return useMutation<Run, ApiError, StartRunBody>({
    mutationFn: api.startRun,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["runs"] });
    },
  });
}

export function useStopRun() {
  const qc = useQueryClient();
  return useMutation<StopResult, ApiError, string>({
    mutationFn: api.stopRun,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["runs"] });
    },
  });
}

/** All recorded events for one run, fetched in pages until the store is exhausted.
 *
 *  Used for a run that has already finished — a live run is followed over SSE instead, so the
 *  two paths never both poll the same data. */
export function useRunEventsAll(runId: string | null, enabled = true) {
  return useQuery<StageEvent[], ApiError>({
    queryKey: ["runs", runId, "events", "all"],
    enabled: !!runId && enabled,
    queryFn: async () => {
      const out: StageEvent[] = [];
      let after = 0;
      // The endpoint caps a page at 2000; loop until a page comes back short.
      for (let page = 0; page < 20; page++) {
        const res = await api.runEvents(runId!, { after_seq: after, limit: 2000 });
        out.push(...res.events);
        if (res.events.length < 2000) break;
        after = res.next_seq;
      }
      return out;
    },
  });
}
