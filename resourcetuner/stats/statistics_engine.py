"""Statistics Engine for ResourceTuner (§6 Policy & Statistics).

Pre-summarizes historical policy records into actionable statistical summaries
for the Decision Agent and Meta-Agent (success rate, EMA improvement, sample count, trend).
"""

from __future__ import annotations

import json
from typing import Any

from resourcetuner.policy.store import PolicyStore


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

            # Compute recent trend
            if sample_count < 2:
                trend = "insufficient_data"
            elif last_outcome == "IMPROVED" and success_rate >= 0.70:
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
