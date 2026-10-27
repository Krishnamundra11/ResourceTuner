import { useQuery } from "@tanstack/react-query";
import { api, type ApiError } from "@/lib/api";
import type { ActionList, LegacyCycleDetail, LegacyCycleList, PolicyList } from "@/lib/types";

/** All three of these read records the pipeline already wrote. The endpoints behind them are
 *  served by dashboard/history.py, which imports nothing that can act — selecting a historical
 *  run cannot rerun an agent, call an LLM, touch a container or change a policy. */

export function useHistoryCycles(params: { limit?: number; offset?: number } = {}) {
  return useQuery<LegacyCycleList, ApiError>({
    queryKey: ["history", "cycles", params],
    queryFn: () => api.historyCycles(params),
    staleTime: 30_000,
  });
}

export function useHistoryCycle(cycle: number | null) {
  return useQuery<LegacyCycleDetail, ApiError>({
    queryKey: ["history", "cycle", cycle],
    queryFn: () => api.historyCycle(cycle!),
    enabled: cycle !== null,
    staleTime: Infinity,          // a historical record never changes
  });
}

export function useHistoryActions(params: { limit?: number; offset?: number } = {}) {
  return useQuery<ActionList, ApiError>({
    queryKey: ["history", "actions", params],
    queryFn: () => api.historyActions(params),
    refetchInterval: 10_000,      // a live run appends to actions.db
  });
}

export function useHistoryPolicies() {
  return useQuery<PolicyList, ApiError>({
    queryKey: ["history", "policies"],
    queryFn: api.historyPolicies,
    refetchInterval: 15_000,
  });
}
