"""SQLite-backed Policy Store for ResourceTuner (§6 Policy Store).

Tracks historical tuning outcomes across (workload, condition, action) tuples
and maintains exponential moving average (EMA) of improvements and success rates.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

DEFAULT_POLICY_DB = Path("data/policy.db")
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
EMA_ALPHA = 0.3


class PolicyStore:
    """Thread-safe SQLite store for action outcomes and learned policies."""

    def __init__(self, db_path: Path | str = DEFAULT_POLICY_DB) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
                conn.executescript(schema_sql)
                conn.commit()

    @staticmethod
    def _param_to_json(param: dict[str, Any] | str) -> str:
        if isinstance(param, str):
            try:
                parsed = json.loads(param)
                return json.dumps(parsed, sort_keys=True)
            except Exception:
                return param
        return json.dumps(param, sort_keys=True)

    def record_outcome(
        self,
        workload: str,
        condition: str,
        action_tool: str,
        action_target: str,
        action_param: dict[str, Any] | str,
        outcome: str,
        improvement_value: float | None = None,
    ) -> dict[str, Any]:
        """Record the outcome of an action.
        
        Outcomes: IMPROVED, DEGRADED, UNCHANGED, TIMEOUT.
        Calculates EMA for improvement_value (alpha=0.3).
        """
        param_json = self._param_to_json(action_param)
        outcome_upper = outcome.upper()
        now = time.time()

        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT success_count, failure_count, avg_improvement, sample_count, promoted
                    FROM policies
                    WHERE workload = ? AND condition = ? AND action_tool = ? AND action_target = ? AND action_param = ?
                    """,
                    (workload, condition, action_tool, action_target, param_json),
                )
                row = cursor.fetchone()

                if row is None:
                    success_count = 1 if outcome_upper == "IMPROVED" else 0
                    failure_count = 1 if outcome_upper in ("DEGRADED", "TIMEOUT") else 0
                    sample_count = 1
                    avg_improvement = improvement_value
                    promoted = 0

                    cursor.execute(
                        """
                        INSERT INTO policies (
                            workload, condition, action_tool, action_target, action_param,
                            success_count, failure_count, avg_improvement, sample_count,
                            promoted, last_outcome, last_updated
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            workload, condition, action_tool, action_target, param_json,
                            success_count, failure_count, avg_improvement, sample_count,
                            promoted, outcome_upper, now,
                        ),
                    )
                else:
                    success_count = row["success_count"] + (1 if outcome_upper == "IMPROVED" else 0)
                    failure_count = row["failure_count"] + (1 if outcome_upper in ("DEGRADED", "TIMEOUT") else 0)
                    sample_count = row["sample_count"] + 1
                    promoted = row["promoted"]

                    current_avg = row["avg_improvement"]
                    if improvement_value is not None:
                        if current_avg is None:
                            avg_improvement = improvement_value
                        else:
                            avg_improvement = round(EMA_ALPHA * improvement_value + (1.0 - EMA_ALPHA) * current_avg, 4)
                    else:
                        avg_improvement = current_avg

                    cursor.execute(
                        """
                        UPDATE policies SET
                            success_count = ?,
                            failure_count = ?,
                            avg_improvement = ?,
                            sample_count = ?,
                            last_outcome = ?,
                            last_updated = ?
                        WHERE workload = ? AND condition = ? AND action_tool = ? AND action_target = ? AND action_param = ?
                        """,
                        (
                            success_count, failure_count, avg_improvement, sample_count,
                            outcome_upper, now,
                            workload, condition, action_tool, action_target, param_json,
                        ),
                    )
                conn.commit()

        return {
            "workload": workload,
            "condition": condition,
            "action_tool": action_tool,
            "action_target": action_target,
            "action_param": param_json,
            "success_count": success_count,
            "failure_count": failure_count,
            "avg_improvement": avg_improvement,
            "sample_count": sample_count,
            "promoted": bool(promoted),
            "last_outcome": outcome_upper,
            "last_updated": now,
        }

    def get_history(self, workload: str, condition: str, top_n: int = 3) -> list[dict[str, Any]]:
        """Fetch up to top_n relevant historical entries for a workload & condition."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT workload, condition, action_tool, action_target, action_param,
                           success_count, failure_count, avg_improvement, sample_count,
                           promoted, last_outcome, last_updated,
                           CASE
                               WHEN workload = ? AND condition = ? THEN 3
                               WHEN condition = ? THEN 2
                               WHEN workload = ? THEN 1
                               ELSE 0
                           END AS relevance
                    FROM policies
                    WHERE (workload = ? AND condition = ?)
                       OR condition = ?
                       OR workload = ?
                    ORDER BY relevance DESC, promoted DESC, sample_count DESC, avg_improvement DESC
                    LIMIT ?
                    """,
                    (workload, condition, condition, workload, workload, condition, condition, workload, top_n),
                )
                rows = cursor.fetchall()
                results = []
                for r in rows:
                    try:
                        param_obj = json.loads(r["action_param"])
                    except Exception:
                        param_obj = r["action_param"]
                    results.append({
                        "workload": r["workload"],
                        "condition": r["condition"],
                        "action_tool": r["action_tool"],
                        "action_target": r["action_target"],
                        "action_param": param_obj,
                        "success_count": r["success_count"],
                        "failure_count": r["failure_count"],
                        "avg_improvement": r["avg_improvement"],
                        "sample_count": r["sample_count"],
                        "promoted": bool(r["promoted"]),
                        "last_outcome": r["last_outcome"],
                        "last_updated": r["last_updated"],
                    })
                return results

    def set_promoted(
        self,
        workload: str,
        condition: str,
        action_tool: str,
        action_target: str,
        action_param: dict[str, Any] | str,
        promoted: bool,
    ) -> bool:
        """Promote or demote an action policy."""
        param_json = self._param_to_json(action_param)
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE policies
                    SET promoted = ?, last_updated = ?
                    WHERE workload = ? AND condition = ? AND action_tool = ? AND action_target = ? AND action_param = ?
                    """,
                    (1 if promoted else 0, time.time(), workload, condition, action_tool, action_target, param_json),
                )
                conn.commit()
                return cursor.rowcount > 0

    def preferred_action(self, workload: str, condition: str) -> dict[str, Any] | None:
        """Return the promoted action if available, or the dominant action with >=10 samples and >=80% success."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                # 1. Check promoted
                cursor.execute(
                    """
                    SELECT workload, condition, action_tool, action_target, action_param,
                           success_count, failure_count, avg_improvement, sample_count,
                           promoted, last_outcome, last_updated
                    FROM policies
                    WHERE workload = ? AND condition = ? AND promoted = 1
                    ORDER BY sample_count DESC, avg_improvement DESC
                    LIMIT 1
                    """,
                    (workload, condition),
                )
                row = cursor.fetchone()
                if row:
                    try:
                        param_obj = json.loads(row["action_param"])
                    except Exception:
                        param_obj = row["action_param"]
                    return {
                        "workload": row["workload"],
                        "condition": row["condition"],
                        "action_tool": row["action_tool"],
                        "action_target": row["action_target"],
                        "action_param": param_obj,
                        "success_count": row["success_count"],
                        "failure_count": row["failure_count"],
                        "avg_improvement": row["avg_improvement"],
                        "sample_count": row["sample_count"],
                        "promoted": True,
                        "last_outcome": row["last_outcome"],
                    }

                # 2. Check candidate with sample_count >= 10 and >= 80% success rate
                cursor.execute(
                    """
                    SELECT workload, condition, action_tool, action_target, action_param,
                           success_count, failure_count, avg_improvement, sample_count,
                           promoted, last_outcome, last_updated,
                           (CAST(success_count AS REAL) / sample_count) as success_rate
                    FROM policies
                    WHERE workload = ? AND condition = ? AND sample_count >= 10
                    ORDER BY success_rate DESC, avg_improvement DESC
                    LIMIT 1
                    """,
                    (workload, condition),
                )
                cand = cursor.fetchone()
                if cand and cand["success_rate"] >= 0.80 and (cand["avg_improvement"] or 0) > 0:
                    try:
                        param_obj = json.loads(cand["action_param"])
                    except Exception:
                        param_obj = cand["action_param"]
                    return {
                        "workload": cand["workload"],
                        "condition": cand["condition"],
                        "action_tool": cand["action_tool"],
                        "action_target": cand["action_target"],
                        "action_param": param_obj,
                        "success_count": cand["success_count"],
                        "failure_count": cand["failure_count"],
                        "avg_improvement": cand["avg_improvement"],
                        "sample_count": cand["sample_count"],
                        "promoted": False,
                        "last_outcome": cand["last_outcome"],
                    }
                return None
