"""Statistics Engine for ResourceTuner (§6 Policy & Statistics).

Pre-summarizes historical policy records into actionable statistical summaries
for the Decision Agent and Meta-Agent (success rate, EMA improvement, sample count, trend).
"""

from __future__ import annotations

import json
from typing import Any

from resourcetuner.policy.store import PolicyStore


MIN_OUTCOMES_FOR_TREND = 4   # need >= 2 per half for a recent-vs-older split to mean anything


def _recent_vs_older_trend(outcomes: list[dict]) -> str | None:
    """A genuine recent-vs-older comparison (brief §7), or None if there isn't enough granular
    history yet to make one meaningful — the caller falls back to a coarser signal in that case.

    `outcomes` must be oldest-first (PolicyStore.get_outcomes's own order). Split in half;
    compare each half's success rate, then (if tied) its mean improvement, to classify whether
    performance is trending up, down, or holding steady."""
    if len(outcomes) < MIN_OUTCOMES_FOR_TREND:
        return None
    mid = len(outcomes) // 2
    older, recent = outcomes[:mid], outcomes[mid:]

    def success_rate(bucket: list[dict]) -> float:
        return sum(1 for o in bucket if o["outcome"] == "IMPROVED") / len(bucket)

    def mean_improvement(bucket: list[dict]) -> float:
        vals = [o["improvement_value"] for o in bucket if o["improvement_value"] is not None]
        return sum(vals) / len(vals) if vals else 0.0

    rate_delta = success_rate(recent) - success_rate(older)
    if rate_delta > 0:
        return "improving"
    if rate_delta < 0:
        return "degrading"
    imp_delta = mean_improvement(recent) - mean_improvement(older)
    if imp_delta > 0.5:
        return "improving"
    if imp_delta < -0.5:
        return "degrading"
    return "stable"


class StatisticsEngine:
    """Computes deterministic statistical summaries of policy history."""

    def __init__(self, store: PolicyStore) -> None:
        self.store = store

    def summarize(self, workload: str, condition: str, top_n: int = 3) -> dict[str, dict[str, Any]]:
        """Return pre-summarized history for the top_n most relevant actions.
        
        Output format:
        {
          "(BUILD,cpu_contention,set_container_cpu_weight,rt-s-indexer,{\"weight\":50})": {
            "success_rate": 0.70,
            "avg_improvement": 8.3,
            "sample_count": 7,
            "recent_trend": "stable",
            "last_outcome": "IMPROVED",
            "promoted": False
          }
        }
        """
        history = self.store.get_history(workload, condition, top_n=top_n)
        summaries: dict[str, dict[str, Any]] = {}

        for rec in history:
            param_json = json.dumps(rec["action_param"], sort_keys=True)
            key = f"({rec['workload']},{rec['condition']},{rec['action_tool']},{rec['action_target']},{param_json})"
            
            sample_count = rec["sample_count"]
            success_count = rec["success_count"]
            success_rate = round(success_count / sample_count, 2) if sample_count > 0 else 0.0
            
            avg_imp = rec["avg_improvement"]
            avg_imp_val = round(avg_imp, 2) if avg_imp is not None else None
            
            last_outcome = rec["last_outcome"]

            # recent_trend (brief §7): a real recent-vs-older comparison when enough granular
            # outcomes have been recorded (PolicyStore.get_outcomes, added alongside this fix);
            # otherwise the coarser last_outcome + lifetime success_rate heuristic this project
            # used before `outcomes` existed — kept as the honest fallback for a policy entry
            # that predates it, not deleted, since there is nothing to compute a split from.
            if sample_count < 2:
                trend = "insufficient_data"
            else:
                outcomes = self.store.get_outcomes(rec["workload"], rec["condition"], rec["action_tool"],
                                                    rec["action_target"], rec["action_param"])
                trend = _recent_vs_older_trend(outcomes)
                if trend is None:
                    if last_outcome == "IMPROVED" and success_rate >= 0.70:
                        trend = "improving"
                    elif last_outcome in ("DEGRADED", "TIMEOUT") or success_rate < 0.40:
                        trend = "degrading"
                    else:
                        trend = "stable"

            summaries[key] = {
                "success_rate": success_rate,
                "avg_improvement": avg_imp_val,
                "sample_count": sample_count,
                "recent_trend": trend,
                "last_outcome": last_outcome,
                "promoted": rec["promoted"],
            }

        return summaries
