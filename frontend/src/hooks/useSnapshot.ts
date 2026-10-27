import { useQuery } from "@tanstack/react-query";
import { api, type ApiError } from "@/lib/api";
import type { Snapshot } from "@/lib/types";

export const SNAPSHOT_INTERVAL_MS = 1000;
const STALE_AFTER_S = 5;

/** Live host/classifier snapshot, polled at 1 Hz (the backend samples at 1 Hz too).
 *  `warmingUp` is the backend's own 503 while the sampler fills its first window. */
export function useSnapshot(enabled = true) {
  const query = useQuery<Snapshot, ApiError>({
    queryKey: ["snapshot"],
    queryFn: api.snapshot,
    refetchInterval: enabled ? SNAPSHOT_INTERVAL_MS : false,
    enabled,
    retry: false,
    // Keep the last good sample on screen while a poll fails, with staleness shown separately.
    placeholderData: (prev) => prev,
  });

  const warmingUp = query.error?.isWarmingUp ?? false;
  const snapshot = query.data ?? null;
  const ageSeconds = snapshot ? Math.max(0, Math.round(Date.now() / 1000 - snapshot.timestamp)) : null;

  return {
    ...query,
    snapshot,
    warmingUp,
    offline: query.error?.status === 0,
    ageSeconds,
    isStale: ageSeconds !== null && ageSeconds > STALE_AFTER_S,
  };
}
