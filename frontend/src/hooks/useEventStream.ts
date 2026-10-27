import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { eventStreamUrl } from "@/lib/api";
import type { StageEvent } from "@/lib/types";

export type StreamState = "connecting" | "open" | "reconnecting" | "closed";

interface Options {
  runId?: string;
  /** Replay from this cursor on first connect (0 = from the beginning of the store). */
  afterSeq?: number;
  enabled?: boolean;
  /** Keep at most this many events in memory; the API paginates for anything older. */
  maxEvents?: number;
}

/** Subscribes to /api/events/stream.
 *
 *  `EventSource` reconnects on its own and re-sends the last `id:` it saw as `Last-Event-ID`,
 *  which the backend honours — so a dropped connection resumes exactly where it stopped. We
 *  still de-duplicate by `seq`, because a resumed stream may legitimately repeat the boundary
 *  event if the browser retried before processing it. */
export function useEventStream({ runId, afterSeq = 0, enabled = true, maxEvents = 2000 }: Options = {}) {
  const [events, setEvents] = useState<StageEvent[]>([]);
  const [state, setState] = useState<StreamState>(enabled ? "connecting" : "closed");
  const [lastError, setLastError] = useState<string | null>(null);
  const seen = useRef<Set<number>>(new Set());
  const cursor = useRef<number>(afterSeq);
  const sourceRef = useRef<EventSource | null>(null);
  const [nonce, setNonce] = useState(0);

  const reset = useCallback(() => {
    seen.current = new Set();
    cursor.current = afterSeq;
    setEvents([]);
    setNonce((n) => n + 1);
  }, [afterSeq]);

  useEffect(() => {
    if (!enabled) { setState("closed"); return; }
    // Resume from the newest event already held, so a remount does not replay the whole store.
    const url = eventStreamUrl({ runId, afterSeq: cursor.current });
    const es = new EventSource(url);
    sourceRef.current = es;
    setState((s) => (s === "open" ? "reconnecting" : "connecting"));

    es.onopen = () => { setState("open"); setLastError(null); };
    es.onerror = () => {
      // EventSource retries by itself; surface it as reconnecting rather than a hard failure.
      setState((s) => (s === "closed" ? s : "reconnecting"));
      setLastError("stream interrupted — reconnecting");
    };
    es.addEventListener("stage", (ev) => {
      const msg = ev as MessageEvent<string>;
      let parsed: StageEvent;
      try { parsed = JSON.parse(msg.data) as StageEvent; } catch { return; }
      if (seen.current.has(parsed.seq)) return;          // duplicate across a reconnect
      seen.current.add(parsed.seq);
      cursor.current = Math.max(cursor.current, parsed.seq);
      setEvents((prev) => {
        const next = [...prev, parsed];
        next.sort((a, b) => a.seq - b.seq);
        return next.length > maxEvents ? next.slice(next.length - maxEvents) : next;
      });
    });

    return () => { es.close(); sourceRef.current = null; setState("closed"); };
  }, [runId, enabled, maxEvents, nonce]);

  const byRun = useMemo(() => {
    const map = new Map<string, StageEvent[]>();
    for (const e of events) {
      const list = map.get(e.run_id) ?? [];
      list.push(e);
      map.set(e.run_id, list);
    }
    return map;
  }, [events]);

  return { events, byRun, state, lastError, lastSeq: cursor.current, reset };
}

/** Collapses an event list into the latest status per (cycle, stage) — what the pipeline graph
 *  draws. A stage that only ever emitted `running` stays `running`; nothing is inferred. */
export function latestStageStatus(events: StageEvent[], cycle: number | null) {
  const map = new Map<string, StageEvent>();
  for (const e of events) {
    if (cycle !== null && e.cycle !== null && e.cycle !== cycle) continue;
    const prev = map.get(e.stage);
    if (!prev || e.seq > prev.seq) map.set(e.stage, e);
  }
  return map;
}
