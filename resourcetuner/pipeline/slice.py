"""The vertical slice as a callable module (Step 9's CLI logic, now drivable by the dashboard).

`scripts/run_vertical_slice.py` is a thin CLI over this; the dashboard's run manager drives the
same class. Both therefore exercise one implementation, and both record the same structured
events, so a CLI run is as replayable as a dashboard run.

What is unchanged from the CLI: the stage order, the warm-up/before/after measurement scheme,
the agents, the Safety Gate and Resource Controller pathways, and the per-cycle JSONL log.
What is added: a `RunEventSink` (one event per stage, emitted only once the stage's result is in
hand), cooperative cancellation between stages, and thresholds read from config/thresholds.yaml
instead of literals.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from resourcetuner.pipeline import stages as S

REPO = Path(__file__).resolve().parents[2]
log = logging.getLogger("resourcetuner.slice")

DEFAULT_LOG_FILE = REPO / "data" / "vertical_slice_log.jsonl"
THRESHOLDS_PATH = REPO / "config" / "thresholds.yaml"


class Cancelled(Exception):
    """Raised between stages when a stop was requested."""


class NullSink:
    """Used when nobody is listening; keeps the pipeline code free of `if sink` branches."""

    def emit(self, *a, **k) -> int:
        return 0

    def stage_start(self, *a, **k) -> int:
        return 0

    def stage_done(self, *a, **k) -> int:
        return 0


@dataclass
class SliceConfig:
    cycles: int = 5
    scenario: str = "build_vs_indexer"
    level: str | None = "high"
    baseline_duration: float = 37.38          # reference benchmark only; each cycle measures its own before
    cooldown_s: float = 20.0
    log_file: Path = DEFAULT_LOG_FILE
    start_scenario: bool = True
    build_container_name: str | None = None
    build_timeout_s: float = 180.0
    trigger_wait_timeout_s: float = 600.0     # give up waiting for contention after this long
    condition: str = "cpu_contention"
    workload_for_policy: str = "BUILD"
    # brief §7: 1-in-5 exploration rule. DEFERRED, not implemented: `force_explore` below is
    # rolled, recorded in the cycle log and the evidence dict, and logged — but nothing reads
    # it. `project_evidence()` (diagnostic_crew/evidence.py, ALWAYS=("trigger","workload")) and
    # decision_summary() (adk/decision.py, 4 fixed keys) both drop it before it reaches any
    # agent, so no agent has ever actually been told to explore. Implementing this for real means
    # deciding — against the original brief text, not re-derivable from this code — what
    # "exploration" changes about the Decision/Diagnostic agents' behaviour (e.g. suppress the
    # "prefer high-success-rate/promoted" preference in _BASE_INSTRUCTION and force a different
    # valid candidate, or something else); guessing that and wiring it up silently would be
    # exactly the kind of unverified behaviour this flag must not pretend to have.
    # See tests/test_step9_vertical_slice.py::test_force_explore_is_recorded_but_not_yet_consumed.
    explore_probability: float = 0.20
    params: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.build_container_name:
            self.build_container_name = f"rt-{self.scenario}-build"

    def thresholds(self) -> dict[str, float]:
        """Contention thresholds from config/thresholds.yaml (no literals in the pipeline)."""
        raw = yaml.safe_load(THRESHOLDS_PATH.read_text(encoding="utf-8")) or {}
        c = raw.get("contention", {})
        return {
            "cpu_psi": float(c.get("cpu", {}).get("psi_some_avg10", 15.6)),
            "mem_psi": float(c.get("memory", {}).get("psi_some_avg10", 10.0)),
            "mem_avail_min_pct": float(c.get("memory", {}).get("available_min_percent", 10.0)),
            "io_psi": float(c.get("io", {}).get("psi_some_avg10", 18.4)),
            "persistence_s": int(c.get("persistence_s", 10)),
        }


class VerticalSlice:
    """One experiment: start the scenario, then run `cycles` tuning cycles on real contention."""

    def __init__(self, config: SliceConfig, sink: Any | None = None,
                 cancel: threading.Event | None = None, docker_client: Any | None = None):
        self.cfg = config
        self.sink = sink or NullSink()
        self.cancel = cancel or threading.Event()
        self.th = config.thresholds()
        self._docker_client = docker_client
        self._started_scenario = False
        self.cycle_logs: list[dict] = []

    # --- lifecycle ------------------------------------------------------------------------

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("stop requested")

    def _setup(self) -> None:
        import docker

        from resourcetuner.classifier.live import LiveClassifier
        from resourcetuner.controller.resource_controller import ActionStore, ControllerGateContext, ResourceController
        from resourcetuner.evaluation.engine import EvaluationEngine
        from resourcetuner.mcp_server.tools import DockerTargetSource, ToolHandlers
        from resourcetuner.policy.store import PolicyStore
        from resourcetuner.safety.gate import SafetyConfig, SafetyGate
        from resourcetuner.stats.statistics_engine import StatisticsEngine
        from resourcetuner.telemetry.host import HostTelemetry

        self.docker_client = self._docker_client or docker.from_env(timeout=30)
        self.telemetry = HostTelemetry(top_n=25)
        self.live = LiveClassifier().start()
        log.info("Waiting for LiveClassifier to accumulate window...")
        self.live.wait_ready(timeout_s=15.0)

        self.action_store = ActionStore(REPO / "data" / "actions.db")
        self.controller = ResourceController(self.action_store)
        self.gate = SafetyGate(SafetyConfig.load(), ControllerGateContext(self.action_store, self.live))
        self.target_source = DockerTargetSource()
        self.handlers = ToolHandlers(self.target_source, self.controller, gate=self.gate)
        self.policy_store = PolicyStore(REPO / "data" / "policy.db")
        self.stats_engine = StatisticsEngine(self.policy_store)
        self.eval_engine = EvaluationEngine(controller=self.controller)

    def _setup_agents(self) -> None:
        from google.adk.runners import InMemoryRunner

        from resourcetuner.agents import llm_router as R
        from resourcetuner.agents.adk.action_guard import DegradedActionBlocker
        from resourcetuner.agents.adk.crew_bridge import make_crew_diagnose
        from resourcetuner.agents.adk.decision import build_decision_agent
        from resourcetuner.agents.adk.meta import build_meta_agent
        from resourcetuner.agents.adk.model_plugin import ModelFallbackPlugin, gemini_model_for
        from resourcetuner.agents.adk.supervisor import build_supervisor

        R.load_dotenv()
        self.llm_cfg = yaml.safe_load((REPO / "config" / "llm.yaml").read_text(encoding="utf-8"))
        self.supervisor_agent = build_supervisor(gemini_model_for(self.llm_cfg, "supervisor"), make_crew_diagnose())
        self.decision_agent = build_decision_agent(gemini_model_for(self.llm_cfg, "decision"),
                                                   safety_cfg=self.gate.cfg)
        self.meta_agent = build_meta_agent(gemini_model_for(self.llm_cfg, "meta"))
        self.runner = InMemoryRunner(agent=self.supervisor_agent, app_name="rt-slice",
                                     plugins=[ModelFallbackPlugin(self.llm_cfg), DegradedActionBlocker()])

    def _start_scenario(self) -> dict:
        import sys
        sys.path.insert(0, str(REPO / "scripts"))
        import run_scenario as rs

        sc = rs.load_scenario(self.cfg.scenario, self.cfg.level)
        started = rs.start_scenario(self.docker_client, sc, replace=True)
        self._started_scenario = True
        names = [c.name for c in started]
        log.info("Started scenario %s (level=%s): %s", self.cfg.scenario, self.cfg.level, ", ".join(names))
        return {"scenario": self.cfg.scenario, "level": self.cfg.level, "containers": names,
                "description": sc.get("description", "")}

    def _teardown(self) -> dict:
        """Restore resources first, then stop containers. Never masks the original failure."""
        result: dict[str, Any] = {}
        try:
            unresolved = self.controller.rollback_unresolved()
            if unresolved:
                log.warning("Rolled back %d unresolved action(s) during cleanup", len(unresolved))
            result["rolled_back"] = unresolved
        except Exception as e:                                        # noqa: BLE001
            log.error("Cleanup rollback failed: %s", e)
            result["rollback_error"] = str(e)[:300]
        try:
            self.live.stop()
        except Exception:                                             # noqa: BLE001
            pass
        if self._started_scenario:
            try:
                import sys
                sys.path.insert(0, str(REPO / "scripts"))
                import run_scenario as rs
                removed = rs.stop_all(self.docker_client)
                log.info("Stopped %d scenario container(s)", removed)
                result["containers_removed"] = removed
            except Exception as e:                                    # noqa: BLE001
                log.error("Could not stop scenario containers: %s", e)
                result["container_stop_error"] = str(e)[:300]
        return result

    # --- builds ---------------------------------------------------------------------------

    def _latest_build(self) -> dict | None:
        try:
            container = self.docker_client.containers.get(self.cfg.build_container_name)
            raw = container.logs(tail=30).decode("utf-8", errors="replace")
            for line in reversed(raw.splitlines()):
                line = line.strip()
                if not line or "duration_s" not in line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("event") == "build" and "iteration" in rec:
                        return rec
                except ValueError:
                    continue
        except Exception as e:                                        # noqa: BLE001
            log.warning("Could not read logs from %s: %s", self.cfg.build_container_name, e)
        return None

    def _wait_for_build(self, after_iteration: int, timeout_s: float | None = None,
                        poll_interval: float = 1.5) -> dict | None:
        deadline = time.monotonic() + (timeout_s or self.cfg.build_timeout_s)
        while time.monotonic() < deadline:
            self._check_cancel()
            rec = self._latest_build()
            if rec and rec.get("iteration", 0) > after_iteration:
                return rec
            time.sleep(poll_interval)
        return None

    def _indexer_weights(self) -> dict[str, int | None]:
        from resourcetuner.controller.resource_controller import find_cgroup_dir
        out = {}
        for t in self.target_source.list():
            if self.cfg.scenario not in t.name and "indexer" not in t.name:
                continue
            try:
                cg = find_cgroup_dir(t.container_id)
                out[t.target_id] = int((cg / "cpu.weight").read_text().strip()) if cg else None
            except Exception:                                         # noqa: BLE001
                out[t.target_id] = None
        return out

    # --- evidence -------------------------------------------------------------------------

    def _build_evidence(self, host_snap, trigger_info: dict, history: list[dict]) -> dict:
        c = self.live.latest()
        ctrs_snap = self.live._ctrs.sample() if hasattr(self.live, "_ctrs") else None
        by_id = {ctr.id: ctr for ctr in (ctrs_snap.containers if ctrs_snap else [])}

        targets = []
        for t in self.target_source.list():
            cs = by_id.get(t.container_id)
            targets.append({
                "target_id": t.target_id,
                "container_id": t.container_id,
                "name": t.name,
                "workload": self.live.workload_of(t.name) or "UNKNOWN",
                "state": t.state,
                "cpu_percent": (cs.cpu_percent or 0.0) if cs else 0.0,
                "cpu_psi": (cs.cpu_psi.some_avg10 if cs and cs.cpu_psi else 0.0),
                "cpu_weight": t.cpu_weight or 100,
                "cpu_limit_cores": t.cpu_limit_cores,
                "mem_mib": round((cs.memory_working_set_bytes or 0) / 2 ** 20, 1) if cs else 0.0,
                "io_read_mib_s": round((cs.io_read_bytes_per_s or 0.0) / 2 ** 20, 2) if cs else 0.0,
                "io_write_mib_s": round((cs.io_write_bytes_per_s or 0.0) / 2 ** 20, 2) if cs else 0.0,
                "oom_kills_delta": (cs.memory_oom_kills or 0) if cs else 0,
            })

        return {
            "trigger": trigger_info,
            "workload": {"class": c.workload if c else self.cfg.workload_for_policy,
                         "confidence": c.confidence if (c and c.confidence is not None) else 1.0},
            "host": {
                "cpu_percent": host_snap.cpu.percent,
                "cpu_psi": host_snap.cpu.psi.some_avg10 if host_snap.cpu.psi else 0.0,
                "mem_avail_pct": round(100 * host_snap.memory.available_bytes / host_snap.memory.total_bytes, 2),
                "mem_psi": host_snap.memory.psi.some_avg10 if host_snap.memory.psi else 0.0,
                "io_psi": host_snap.disk.psi.some_avg10 if host_snap.disk.psi else 0.0,
                "iowait": host_snap.cpu.iowait,
                "cores": host_snap.cpu.core_count,
            },
            "thresholds": {k: v for k, v in self.th.items() if k != "persistence_s"},
            "targets": targets,
            "history": history,
        }

    # --- the run --------------------------------------------------------------------------

    async def arun(self) -> dict:
        summary: dict[str, Any] = {"cycles_done": 0, "status": S.RUN_COMPLETED, "cycles": []}
        self.cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._setup()
            if self.cfg.start_scenario:
                self.sink.stage_start(S.SCENARIO_START, summary=f"starting {self.cfg.scenario}")
                info = self._start_scenario()
                self.sink.stage_done(S.SCENARIO_START, summary=f"{len(info['containers'])} container(s) running",
                                     payload=info)
                time.sleep(5.0)
            else:
                self.sink.emit(S.SCENARIO_START, S.SKIPPED, summary="using already-running containers")
            self._setup_agents()

            while summary["cycles_done"] < self.cfg.cycles:
                self._check_cancel()
                trigger_info = self._wait_for_trigger()
                cycle_num = summary["cycles_done"] + 1
                host_snap = self.telemetry.sample()
                history = self.policy_store.get_history(self.cfg.workload_for_policy, self.cfg.condition, top_n=3)
                evidence = self._build_evidence(host_snap, trigger_info, history)

                cycle_log = await self._run_cycle(cycle_num, evidence)
                self.cycle_logs.append(cycle_log)
                summary["cycles"].append({k: cycle_log.get(k) for k in
                                          ("cycle", "status", "decision", "execution", "evaluation")})
                summary["cycles_done"] = cycle_num

                if summary["cycles_done"] < self.cfg.cycles:
                    log.info("Entering cooldown (%ds)...", int(self.cfg.cooldown_s))
                    self._sleep_cancellable(self.cfg.cooldown_s)

            log.info("ALL %d CYCLES COMPLETED SUCCESSFULLY!", summary["cycles_done"])
        except Cancelled as e:
            summary["status"] = S.RUN_CANCELLED
            summary["error"] = str(e)
            log.warning("Run cancelled after %d cycle(s)", summary["cycles_done"])
        except Exception as e:                                        # noqa: BLE001
            summary["status"] = S.RUN_FAILED
            summary["error"] = f"{type(e).__name__}: {e}"
            log.exception("Run failed")
        finally:
            summary["cleanup"] = self._teardown()
            status = {S.RUN_COMPLETED: S.COMPLETED, S.RUN_CANCELLED: S.SKIPPED,
                      S.RUN_FAILED: S.FAILED}[summary["status"]]
            self.sink.emit(S.RUN_SUMMARY, status,
                           summary=f"{summary['cycles_done']}/{self.cfg.cycles} cycle(s), {summary['status']}",
                           payload=summary)
        return summary

    def _sleep_cancellable(self, seconds: float) -> None:
        if self.cancel.wait(timeout=seconds):
            raise Cancelled("stop requested during cooldown")

    def _wait_for_trigger(self) -> dict:
        """Watch host CPU PSI until it stays above the calibrated threshold for `persistence_s`."""
        threshold, persist = self.th["cpu_psi"], self.th["persistence_s"]
        self.sink.stage_start(S.TRIGGER,
                              summary=f"watching for cpu_psi >= {threshold}% for {persist}s",
                              payload={"metric": "host.cpu_psi", "threshold": threshold,
                                       "persistence_s": persist})
        window: deque[float] = deque(maxlen=persist)
        deadline = time.monotonic() + self.cfg.trigger_wait_timeout_s
        while True:
            self._check_cancel()
            if time.monotonic() > deadline:
                self.sink.stage_done(S.TRIGGER, S.FAILED,
                                     summary=f"no contention for {self.cfg.trigger_wait_timeout_s:.0f}s")
                raise TimeoutError(f"no contention within {self.cfg.trigger_wait_timeout_s:.0f}s "
                                   f"(cpu_psi never held >= {threshold}% for {persist}s)")
            snap = self.telemetry.sample()
            psi = snap.cpu.psi.some_avg10 if snap.cpu.psi else 0.0
            window.append(psi)
            if len(window) == persist and all(p >= threshold for p in window):
                log.info("TRIGGER DETECTED: cpu_psi >= %.1f%% for %ds (latest=%.1f%%)", threshold, persist, psi)
                info = {"resource": "cpu", "metric": "host.cpu_psi", "value": psi,
                        "threshold": threshold, "persisted_s": persist}
                self.sink.stage_done(S.TRIGGER, summary=f"cpu_psi {psi:.1f}% >= {threshold}% for {persist}s",
                                     payload=info)
                return info
            time.sleep(1.0)

    async def _run_cycle(self, cycle_num: int, evidence: dict) -> dict:
        """One tuning cycle. Stage order and semantics are Step 9's; events are new."""
        import json as _json

        from google.genai import types

        from resourcetuner.agents.adk.action_guard import actions_for_decision
        from resourcetuner.agents.adk.decision import decision_summary, execute_decision, parse_decision_output
        from resourcetuner.agents.adk.supervisor import supervisor_summary

        log.info("==================== STARTING VERTICAL SLICE CYCLE %d ====================", cycle_num)
        t0 = time.time()
        C = cycle_num
        cl: dict[str, Any] = {"cycle": C, "timestamp": t0, "trigger": evidence.get("trigger"),
                              "reference_baseline_s": self.cfg.baseline_duration, "status": "running"}

        # --- classification (already computed by the live classifier; recorded for the UI) ---
        wl = evidence.get("workload", {})
        self.sink.emit(S.CLASSIFY, S.COMPLETED, cycle=C,
                       summary=f"{wl.get('class')} (confidence {wl.get('confidence')})",
                       payload={"workload": wl, "host": evidence.get("host"),
                                "targets": [{k: t.get(k) for k in ("target_id", "workload", "cpu_percent",
                                                                   "cpu_weight", "state")}
                                            for t in evidence.get("targets", [])]})

        # --- STEP 1+2: warm-up build, then the actual before-measurement ---
        self.sink.stage_start(S.WARMUP, cycle=C, summary="throwaway warm-up build (discarded)")
        cur = self._latest_build()
        cur_iter = cur.get("iteration", 0) if cur else 0
        warm = self._wait_for_build(cur_iter)
        if not warm or not warm.get("success", False):
            self.sink.stage_done(S.WARMUP, S.FAILED, cycle=C, summary="warm-up build failed or timed out",
                                 payload={"record": warm})
            cl.update(status="failed_warmup", error="Warm-up build failed")
            return self._finish_cycle(cl, t0)
        self.sink.stage_done(S.WARMUP, cycle=C, summary=f"warm-up {warm['duration_s']:.2f}s (discarded)",
                             payload={"record": warm})
        cl["warm_up_build"] = warm

        self.sink.stage_start(S.BASELINE, cycle=C, summary="measuring this cycle's before-build")
        before = self._wait_for_build(warm["iteration"])
        if not before or not before.get("success", False):
            self.sink.stage_done(S.BASELINE, S.FAILED, cycle=C, summary="before-measurement failed or timed out",
                                 payload={"record": before})
            cl.update(status="failed_before_measurement", error="Before-measurement failed")
            return self._finish_cycle(cl, t0)
        before_dur, before_iter = float(before["duration_s"]), before["iteration"]
        self.sink.stage_done(S.BASELINE, cycle=C,
                             summary=f"before = {before_dur:.2f}s (ref {self.cfg.baseline_duration:.2f}s)",
                             payload={"record": before, "reference_baseline_s": self.cfg.baseline_duration})
        cl["before_build"] = before

        cl["indexer_initial_weights"] = self._indexer_weights()
        log.info("Initial indexer cgroup weights: %s", cl["indexer_initial_weights"])

        # Exploration rule (brief §7) — DEFERRED, see SliceConfig.explore_probability above.
        # Recorded in the cycle log and on `evidence` for visibility, but no agent reads it.
        cl["force_explore"] = random.random() < self.cfg.explore_probability
        if cl["force_explore"]:
            evidence["force_explore"] = True
            log.info("Exploration rule triggered: force_explore=True (1-in-5) — recorded only, not yet acted on")

        # --- STEP 4: Supervisor (fresh session per agent; see test_adk_runner_agent_... regression) ---
        self._check_cancel()
        self.sink.stage_start(S.SUPERVISOR, cycle=C, summary="choosing diagnostic specialists")
        self.runner.agent = self.supervisor_agent
        session = await self.runner.session_service.create_session(
            app_name="rt-slice", user_id="vertical_slice", state={"evidence": evidence})
        sup_msg = types.Content(role="user", parts=[types.Part(
            text="Deterministic trigger summary: " + _json.dumps(supervisor_summary(evidence)))])
        async for _ in self.runner.run_async(user_id="vertical_slice", session_id=session.id,
                                             new_message=sup_msg):
            pass
        state = (await self.runner.session_service.get_session(
            app_name="rt-slice", user_id="vertical_slice", session_id=session.id)).state
        delegation = state.get("delegation") or {}
        findings = state.get("findings") or []
        degraded = bool(state.get("llm_degraded"))
        prov = state.get("llm_provenance")
        cl.update(delegation=delegation, llm_degraded=degraded, llm_provenance=prov, findings=findings)
        log.info("Supervisor completed: delegated to %s (degraded=%s, reason=%s)",
                 delegation.get("resources"), degraded, delegation.get("reason"))
        self.sink.stage_done(S.SUPERVISOR, cycle=C,
                             summary=f"delegated to {delegation.get('resources')}",
                             payload={"delegation": delegation, "provenance": prov, "degraded": degraded})

        # --- diagnostics: findings returned by the crew through the Supervisor's one tool ---
        if findings:
            self.sink.emit(S.DIAGNOSTICS, S.COMPLETED, cycle=C,
                           summary="; ".join(f"{f.get('agent')}: "
                                             f"{'contention' if (f.get('diagnosis') or {}).get('contention') else 'no contention'}"
                                             f"{'' if f.get('check_ok') else ' (checks failed)'}" for f in findings),
                           payload={"findings": findings})
        else:
            self.sink.emit(S.DIAGNOSTICS, S.SKIPPED, cycle=C, summary="no specialist was consulted")

        # --- STEP 5: Decision Agent ---
        self._check_cancel()
        candidates = actions_for_decision(findings, degraded)
        wl_class = wl.get("class", self.cfg.workload_for_policy)
        stats_summary = self.stats_engine.summarize(wl_class, self.cfg.condition)
        log.info("Decision stage: %d candidate action(s), %d policy history record(s)",
                 len(candidates), len(stats_summary))
        self.sink.stage_start(S.DECISION, cycle=C,
                              summary=f"{len(candidates)} candidate(s), {len(stats_summary)} history record(s)",
                              payload={"candidates": candidates, "history_statistics": stats_summary})

        self.runner.agent = self.decision_agent
        dec_session = await self.runner.session_service.create_session(
            app_name="rt-slice", user_id="vertical_slice",
            state={"evidence": evidence, "llm_degraded": degraded})
        dec_msg = types.Content(role="user", parts=[types.Part(
            text="Decision input: " + _json.dumps(decision_summary(evidence, candidates, stats_summary)))])
        dec_text = ""
        async for ev in self.runner.run_async(user_id="vertical_slice", session_id=dec_session.id,
                                              new_message=dec_msg):
            if ev.is_final_response() and ev.content and ev.content.parts:
                dec_text = "".join(p.text or "" for p in ev.content.parts)
        decision = parse_decision_output(dec_text)
        cl["decision"] = decision.model_dump()
        log.info("Decision Agent verdict: %s | Reasoning: %s", decision.chosen_action, decision.reasoning)
        act = decision.chosen_action
        self.sink.stage_done(S.DECISION, S.COMPLETED if act else S.SKIPPED, cycle=C,
                             summary=(f"{act.tool} on {act.target_id} "
                                      f"({'weight ' + str(act.weight) if act.weight is not None else 'cpus ' + str(act.cpus)})"
                                      if act else "no_action"),
                             payload=cl["decision"])

        # --- STEP 6: Safety Gate + Resource Controller (unchanged pathway) ---
        self._check_cancel()
        self.sink.stage_start(S.SAFETY_GATE, cycle=C,
                              summary="evaluating the proposed action" if act else "nothing to evaluate")
        exec_result = execute_decision(decision, self.handlers, llm_degraded=degraded)
        cl["execution"] = exec_result
        status = exec_result.get("status")
        action_id = exec_result.get("action_id")
        log.info("Action execution result: %s (action_id=%s)", status, action_id)

        gate_status = {"applied": S.COMPLETED, "rejected": S.REJECTED, "blocked": S.REJECTED,
                       "no_action": S.SKIPPED}.get(status, S.FAILED)
        self.sink.stage_done(S.SAFETY_GATE, gate_status, cycle=C,
                             summary=(f"REJECTED: {'; '.join(exec_result.get('reasons', []))}"
                                      if status in ("rejected", "blocked")
                                      else {"applied": "approved", "no_action": "no action proposed"}
                                      .get(status, str(status))),
                             payload=exec_result)

        cl["indexer_post_action_weights"] = self._indexer_weights()
        log.info("Post-action indexer cgroup weights: %s", cl["indexer_post_action_weights"])

        if status == "applied" and action_id:
            self.sink.emit(S.CONTROLLER, S.COMPLETED, cycle=C,
                           summary=f"{exec_result.get('knob')} {exec_result.get('old_value')} -> "
                                   f"{exec_result.get('observed', exec_result.get('new_value'))} (cgroup verified)",
                           payload={"execution": exec_result,
                                    "cgroup_readback": cl["indexer_post_action_weights"]})
        else:
            self.sink.emit(S.CONTROLLER, S.SKIPPED, cycle=C, summary="nothing was applied")

        # --- STEP 7-9: after-measurement, evaluation, policy, meta ---
        if status == "applied" and action_id:
            cl = await self._evaluate_and_learn(cl, C, action_id, before_dur, before_iter, wl_class, decision)
        else:
            for stage in (S.POST_MEASURE, S.EVALUATION, S.POLICY, S.META):
                self.sink.emit(stage, S.SKIPPED, cycle=C, summary="no action was applied this cycle")

        cl["status"] = "completed"
        return self._finish_cycle(cl, t0)

    async def _evaluate_and_learn(self, cl: dict, C: int, action_id: str, before_dur: float,
                                  before_iter: int, wl_class: str, decision) -> dict:
        import json as _json

        from google.genai import types

        from resourcetuner.agents.adk.meta import evaluate_policy_candidate, parse_meta_decision

        self.sink.stage_start(S.POST_MEASURE, cycle=C, summary="measuring the after-build")
        after = self._wait_for_build(before_iter)
        cl["after_build"] = after

        if not after or not after.get("success", False):
            log.error("After-measurement build failed or timed out: %s", after)
            self.sink.stage_done(S.POST_MEASURE, S.FAILED, cycle=C,
                                 summary="after-measurement failed or timed out", payload={"record": after})
            self.sink.stage_start(S.EVALUATION, cycle=C, summary="no after-measurement: rolling back")
            result = self.eval_engine.evaluate_discrete(
                action_id=action_id, before_duration=before_dur, after_duration=None,
                action_timestamp=time.time(), reference_baseline=self.cfg.baseline_duration,
                container_name=self.cfg.build_container_name)
        else:
            after_dur = float(after["duration_s"])
            log.info("Actual after-measurement: duration=%.2fs (iteration=%d) vs before %.2fs",
                     after_dur, after["iteration"], before_dur)
            self.sink.stage_done(S.POST_MEASURE, cycle=C, summary=f"after = {after_dur:.2f}s",
                                 payload={"record": after})
            self.sink.stage_start(S.EVALUATION, cycle=C,
                                  summary=f"comparing {after_dur:.2f}s against {before_dur:.2f}s")
            result = self.eval_engine.evaluate_discrete(
                action_id=action_id, before_duration=before_dur, after_duration=after_dur,
                reference_baseline=self.cfg.baseline_duration,
                container_name=self.cfg.build_container_name)

        cl["evaluation"] = result.to_dict()
        log.info("Evaluation Verdict: %s (improvement=%.2f%%, detail=%s, rolled_back=%s)",
                 result.verdict, result.improvement_pct or 0.0, result.detail, result.rolled_back)
        final_weights = self._indexer_weights()
        cl["indexer_final_weights"] = final_weights
        log.info("Final indexer cgroup weights after evaluation resolution: %s", final_weights)
        self.sink.stage_done(S.EVALUATION,
                             S.COMPLETED if result.verdict in ("IMPROVED", "UNCHANGED") else S.REJECTED,
                             cycle=C,
                             summary=f"{result.verdict} ({result.improvement_pct}%)"
                                     f"{', rolled back' if result.rolled_back else ', kept'}",
                             payload={**cl["evaluation"], "cgroup_readback": final_weights})

        action = decision.chosen_action
        if not action:
            return cl
        param = {"weight": action.weight} if action.weight is not None else {"cpus": action.cpus}
        rec = self.policy_store.record_outcome(
            workload=wl_class, condition=self.cfg.condition, action_tool=action.tool,
            action_target=action.target_id, action_param=param, outcome=result.verdict,
            improvement_value=result.improvement_pct)
        cl["policy_record"] = rec
        log.info("PolicyStore record: sample_count=%d, success_count=%d, failure_count=%d, "
                 "avg_improvement=%.2f, last_outcome=%s",
                 rec["sample_count"], rec["success_count"], rec["failure_count"],
                 rec["avg_improvement"] or 0.0, rec["last_outcome"])
        self.sink.emit(S.POLICY, S.COMPLETED, cycle=C,
                       summary=f"{rec['success_count']}/{rec['sample_count']} successes, "
                               f"EMA {rec['avg_improvement']}", payload=rec)

        call_llm, skip_reason = evaluate_policy_candidate(rec, llm_degraded=cl.get("llm_degraded", False))
        if not call_llm:
            log.info("Meta-Agent skipped: %s", skip_reason)
            cl["meta"] = {"promote": False, "reasoning": skip_reason}
            self.sink.emit(S.META, S.SKIPPED, cycle=C, summary=skip_reason or "deterministic floor not met",
                           payload=cl["meta"])
            return cl

        self.sink.stage_start(S.META, cycle=C, summary="policy promotion reasoning")
        self.runner.agent = self.meta_agent
        meta_session = await self.runner.session_service.create_session(
            app_name="rt-slice", user_id="vertical_slice")
        meta_msg = types.Content(role="user", parts=[types.Part(
            text="Evaluate policy promotion: " + _json.dumps(rec))])
        meta_text = ""
        async for ev in self.runner.run_async(user_id="vertical_slice", session_id=meta_session.id,
                                              new_message=meta_msg):
            if ev.is_final_response() and ev.content and ev.content.parts:
                meta_text = "".join(p.text or "" for p in ev.content.parts)
        meta = parse_meta_decision(meta_text)
        cl["meta"] = meta.model_dump()
        log.info("Meta-Agent decision: promote=%s | Reason: %s", meta.promote, meta.reasoning)
        if meta.promote:
            self.policy_store.set_promoted(workload=wl_class, condition=self.cfg.condition,
                                           action_tool=action.tool, action_target=action.target_id,
                                           action_param=param, promoted=True)
            log.info("POLICY PROMOTED TO DEFAULT STORE!")
        self.sink.stage_done(S.META, cycle=C,
                             summary=f"promote={meta.promote}: {meta.reasoning[:160]}", payload=cl["meta"])
        return cl

    def _finish_cycle(self, cl: dict, t0: float) -> dict:
        cl["duration_s"] = round(time.time() - t0, 2)
        with open(self.cfg.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(cl, default=str) + "\n")
        self.sink.emit(S.CYCLE_SUMMARY,
                       S.COMPLETED if cl.get("status") == "completed" else S.FAILED,
                       cycle=cl["cycle"], summary=f"cycle {cl['cycle']}: {cl.get('status')}",
                       payload={k: cl.get(k) for k in
                                ("cycle", "status", "duration_s", "decision", "execution", "evaluation",
                                 "meta", "error", "indexer_initial_weights", "indexer_post_action_weights",
                                 "indexer_final_weights", "force_explore")})
        return cl
