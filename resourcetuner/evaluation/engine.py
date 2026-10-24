"""Evaluation Engine for ResourceTuner (§6 Evaluation & Rollback).

Measures post-action impact on developer workload performance and decides whether
an action should be kept or rolled back.

Supports two evaluation paradigms (approved decisions 2-A & 2-D):
1. Discrete delta: For bounded tasks (BUILD, TESTING). Compares build/test completion duration
   (measured before vs after intervention) against the discrete thresholds in config/thresholds.yaml:
   <= -10% -> IMPROVED, >= +10% -> DEGRADED, otherwise UNCHANGED.
2. Rolling window: For continuous tasks (CODE_INDEXING, DEV_SERVER). Compares host/target
   pressure stall information (PSI) in a post-action window against the pre-action window:
   <= -15% PSI -> IMPROVED, >= +15% PSI -> DEGRADED, otherwise UNCHANGED.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import time
from typing import Any

import yaml

from resourcetuner.controller.resource_controller import ResourceController

log = logging.getLogger("resourcetuner.evaluation")

DEFAULT_THRESHOLDS_PATH = Path("config/thresholds.yaml")


@dataclass
class EvaluationResult:
    verdict: str  # IMPROVED, UNCHANGED, DEGRADED, TIMEOUT, ERROR
    metric_name: str
    baseline_value: float | None = None   # actual before-measurement (or pre-psi)
    measured_value: float | None = None   # actual after-measurement (or post-psi)
    improvement_pct: float | None = None
    reference_baseline: float | None = None
    rolled_back: bool = False
    detail: str = ""
    final_controller_state: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "metric_name": self.metric_name,
            "baseline_value": self.baseline_value,
            "measured_value": self.measured_value,
            "improvement_pct": self.improvement_pct,
            "reference_baseline": self.reference_baseline,
            "rolled_back": self.rolled_back,
            "detail": self.detail,
            "final_controller_state": self.final_controller_state,
        }


class EvaluationEngine:
    """Evaluates applied actions and triggers rollbacks when degradation occurs."""

    def __init__(
        self,
        controller: ResourceController | None = None,
        thresholds_path: Path | str = DEFAULT_THRESHOLDS_PATH,
    ) -> None:
        self.controller = controller
        self.thresholds_path = Path(thresholds_path)
        self.cfg = self._load_thresholds()

    def _load_thresholds(self) -> dict[str, Any]:
        if not self.thresholds_path.is_file():
            log.warning("Thresholds file not found at %s; using defaults", self.thresholds_path)
            return {}
        try:
            raw = yaml.safe_load(self.thresholds_path.read_text(encoding="utf-8")) or {}
            return raw.get("evaluation", {})
        except Exception as e:
            log.error("Failed to load thresholds from %s: %s", self.thresholds_path, e)
            return {}

    def evaluate_discrete(
        self,
        action_id: str | None,
        before_duration: float | None = None,
        baseline_duration: float | None = None,
        after_duration: float | None = None,
        action_timestamp: float | None = None,
        container_name: str = "rt-build_vs_indexer-build",
        timeout_s: float | None = None,
        poll_interval: float = 2.0,
        docker_client: Any | None = None,
        reference_baseline: float | None = None,
        after_iteration: int | None = None,
    ) -> EvaluationResult:
        """Evaluate discrete workload (e.g. BUILD) by comparing after vs before completion durations."""
        # Allow baseline_duration as alias for before_duration
        actual_before = before_duration if before_duration is not None else baseline_duration
        if actual_before is None or actual_before <= 0:
            return self._finalize(
                action_id,
                EvaluationResult(
                    verdict="ERROR",
                    metric_name="duration_s",
                    baseline_value=actual_before,
                    reference_baseline=reference_baseline,
                    detail="Invalid before_duration provided for discrete evaluation",
                ),
            )

        discrete_cfg = self.cfg.get("discrete", {})
        improved_factor = discrete_cfg.get("improved_threshold", 0.90)  # new <= before * 0.90 (-10%)
        degraded_factor = discrete_cfg.get("degraded_threshold", 1.10)  # new >= before * 1.10 (+10%)
        timeout_factor = discrete_cfg.get("timeout_factor", 2.5)
        timeout_min = discrete_cfg.get("timeout_min_s", 120.0)

        if timeout_s is None:
            timeout_s = max(timeout_min, actual_before * timeout_factor)

        measured_duration: float | None = after_duration

        if measured_duration is None:
            if action_timestamp is None:
                action_timestamp = time.time()
            if docker_client is None:
                try:
                    import docker
                    docker_client = docker.from_env(timeout=30)
                except Exception as e:
                    return self._finalize(
                        action_id,
                        EvaluationResult(
                            verdict="ERROR",
                            metric_name="duration_s",
                            baseline_value=actual_before,
                            reference_baseline=reference_baseline,
                            detail=f"Docker client error: {e}",
                        ),
                    )

            t_start = time.monotonic()
            log.info(
                "Polling container %s for post-action build (actual before: %.2fs, timeout: %.1fs)",
                container_name, actual_before, timeout_s
            )

            build_failed = False
            while time.monotonic() - t_start < timeout_s:
                try:
                    container = docker_client.containers.get(container_name)
                    raw_logs = container.logs(since=int(action_timestamp)).decode("utf-8", errors="replace")
                    for line in reversed(raw_logs.splitlines()):
                        line = line.strip()
                        if not line or "duration_s" not in line:
                            continue
                        try:
                            record = json.loads(line)
                            # Check iteration constraint if provided
                            if after_iteration is not None and record.get("iteration", 0) <= after_iteration:
                                continue
                            if not record.get("success", True):
                                build_failed = True
                                break
                            if "duration_s" in record:
                                measured_duration = float(record["duration_s"])
                                break
                        except Exception:
                            continue
                    if measured_duration is not None or build_failed:
                        break
                except Exception as e:
                    log.warning("Could not fetch logs from %s: %s", container_name, e)

                time.sleep(poll_interval)

            if build_failed:
                res = EvaluationResult(
                    verdict="ERROR",
                    metric_name="duration_s",
                    baseline_value=actual_before,
                    reference_baseline=reference_baseline,
                    detail="Post-action build failed inside container (success=false)",
                )
                return self._finalize(action_id, res)

            if measured_duration is None:
                res = EvaluationResult(
                    verdict="TIMEOUT",
                    metric_name="duration_s",
                    baseline_value=actual_before,
                    reference_baseline=reference_baseline,
                    detail=f"No successful build observed within {timeout_s:.1f}s timeout",
                )
                return self._finalize(action_id, res)

        improvement_pct = round(((actual_before - measured_duration) / actual_before) * 100.0, 2)
        ref_str = f" [ref benchmark: {reference_baseline:.2f}s]" if reference_baseline else ""

        if measured_duration <= round(actual_before * improved_factor, 4):
            verdict = "IMPROVED"
            detail = f"Build completed in {measured_duration:.2f}s vs actual before {actual_before:.2f}s (+{improvement_pct}%){ref_str}"
        elif measured_duration >= round(actual_before * degraded_factor, 4):
            verdict = "DEGRADED"
            detail = f"Build completed in {measured_duration:.2f}s vs actual before {actual_before:.2f}s ({improvement_pct}%){ref_str}"
        else:
            verdict = "UNCHANGED"
            detail = f"Build completed in {measured_duration:.2f}s vs actual before {actual_before:.2f}s ({improvement_pct}% within +-10% noise band){ref_str}"

        res = EvaluationResult(
            verdict=verdict,
            metric_name="duration_s",
            baseline_value=actual_before,
            measured_value=measured_duration,
            improvement_pct=improvement_pct,
            reference_baseline=reference_baseline,
            detail=detail,
        )
        return self._finalize(action_id, res)

    def evaluate_continuous(
        self,
        action_id: str | None,
        pre_psi: float,
        post_psi: float,
    ) -> EvaluationResult:
        """Evaluate continuous workload by comparing post-action PSI against pre-action PSI."""
        cont_cfg = self.cfg.get("continuous", {})
        improved_factor = cont_cfg.get("improved_psi_factor", 0.85)  # post <= pre * 0.85 (-15%)
        degraded_factor = cont_cfg.get("degraded_psi_factor", 1.15)  # post >= pre * 1.15 (+15%)

        if pre_psi > 0:
            improvement_pct = round(((pre_psi - post_psi) / pre_psi) * 100.0, 2)
        else:
            improvement_pct = 0.0

        if post_psi <= round(pre_psi * improved_factor, 4):
            verdict = "IMPROVED"
            detail = f"PSI dropped from {pre_psi:.2f}% to {post_psi:.2f}% (+{improvement_pct}%)"
        elif post_psi >= round(pre_psi * degraded_factor, 4):
            verdict = "DEGRADED"
            detail = f"PSI rose from {pre_psi:.2f}% to {post_psi:.2f}% ({improvement_pct}%)"
        else:
            verdict = "UNCHANGED"
            detail = f"PSI changed from {pre_psi:.2f}% to {post_psi:.2f}% ({improvement_pct}%) within +-15% margin"

        res = EvaluationResult(
            verdict=verdict,
            metric_name="cpu_psi",
            baseline_value=pre_psi,
            measured_value=post_psi,
            improvement_pct=improvement_pct,
            detail=detail,
        )
        return self._finalize(action_id, res)

    def _finalize(self, action_id: str | None, result: EvaluationResult) -> EvaluationResult:
        """Enforce rollback on DEGRADED/TIMEOUT/ERROR, or keep on IMPROVED/UNCHANGED."""
        if self.controller is None or action_id is None:
            return result

        if result.verdict in ("DEGRADED", "TIMEOUT", "ERROR"):
            log.warning("Action %s resulted in %s; initiating controller rollback", action_id, result.verdict)
            rb_res = self.controller.rollback(action_id)
            result.rolled_back = rb_res.get("status") == "rolled_back"
            result.detail += f" | Rolled back: {rb_res}"
        elif result.verdict in ("IMPROVED", "UNCHANGED"):
            keep_res = self.controller.keep(action_id)
            log.info("Action %s confirmed with verdict %s: %s", action_id, result.verdict, keep_res)

        # Independent controller state verification
        if hasattr(self.controller, "store"):
            result.final_controller_state = self.controller.store.get(action_id)

        return result
