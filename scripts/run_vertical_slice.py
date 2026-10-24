#!/usr/bin/env python3
"""Phase 2 Prototype Milestone: End-to-End Vertical Slice (§6, Step 9).

Executes the complete ResourceTuner pipeline under live workload contention:
1. Warm-up throwaway build (ensures warm cache, discarded from eval stats)
2. Actual per-cycle before-measurement
3. Contention trigger verification (10s CPU PSI >= 15.6%)
4. ADK Supervisor -> CrewAI Diagnostic Crew -> ADK Decision Agent
5. Safety Gate -> Resource Controller (with independent cgroup verification)
6. Actual per-cycle after-measurement
7. Evaluation Engine (actual before vs after completion times, +-10% discrete delta)
8. Rollback on DEGRADED/TIMEOUT with independent cgroup verification, or Keep on IMPROVED/UNCHANGED
9. Meta-Agent policy governance -> Policy Store update

Usage:
  ./venv/bin/python scripts/run_vertical_slice.py --cycles 5 --scenario build_vs_indexer --level high --baseline-duration 37.38
"""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
import json
import logging
import os
from pathlib import Path
import random
import sys
import time
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")

import docker
import yaml
from google.adk.runners import InMemoryRunner
from google.genai import types

from resourcetuner.agents import llm_router as R
from resourcetuner.agents.adk import model_plugin as MP
from resourcetuner.agents.adk.action_guard import DegradedActionBlocker, actions_for_decision
from resourcetuner.agents.adk.crew_bridge import make_crew_diagnose
from resourcetuner.agents.adk.decision import (
    build_decision_agent,
    decision_summary,
    execute_decision,
    parse_decision_output,
)
from resourcetuner.agents.adk.meta import (
    build_meta_agent,
    evaluate_policy_candidate,
    parse_meta_decision,
)
from resourcetuner.agents.adk.model_plugin import ModelFallbackPlugin, gemini_model_for
from resourcetuner.agents.adk.supervisor import build_supervisor, supervisor_summary
from resourcetuner.classifier.live import LiveClassifier
from resourcetuner.controller.resource_controller import (
    ActionStore,
    ControllerGateContext,
    ResourceController,
    find_cgroup_dir,
)
from resourcetuner.evaluation.engine import EvaluationEngine, EvaluationResult
from resourcetuner.mcp_server.tools import DockerTargetSource, ToolHandlers
from resourcetuner.policy.store import PolicyStore
from resourcetuner.safety.gate import SafetyConfig, SafetyGate
from resourcetuner.stats.statistics_engine import StatisticsEngine
from resourcetuner.telemetry.host import HostSnapshot, HostTelemetry

log = logging.getLogger("resourcetuner.slice")


def setup_logging():
    import sys
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


def load_config() -> dict[str, Any]:
    R.load_dotenv()
    with open(REPO / "config" / "llm.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_latest_build(container_name: str, docker_client: Any) -> dict[str, Any] | None:
    """Fetch the latest build record from container logs."""
    try:
        container = docker_client.containers.get(container_name)
        raw_logs = container.logs(tail=30).decode("utf-8", errors="replace")
        for line in reversed(raw_logs.splitlines()):
            line = line.strip()
            if not line or "duration_s" not in line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("event") == "build" and "iteration" in rec:
                    return rec
            except Exception:
                continue
    except Exception as e:
        log.warning("Could not read logs from %s: %s", container_name, e)
    return None


def wait_for_build(
    container_name: str,
    after_iteration: int,
    docker_client: Any,
    timeout_s: float = 180.0,
    poll_interval: float = 1.5,
) -> dict[str, Any] | None:
    """Wait for a new build event with iteration > after_iteration."""
    t_start = time.monotonic()
    while time.monotonic() - t_start < timeout_s:
        rec = get_latest_build(container_name, docker_client)
        if rec and rec.get("iteration", 0) > after_iteration:
            return rec
        time.sleep(poll_interval)
    return None


def read_container_cpu_weight(container_id: str) -> int | None:
    """Directly read cgroup v2 cpu.weight for independent verification."""
    try:
        cg = find_cgroup_dir(container_id)
        if cg and (cg / "cpu.weight").is_file():
            return int((cg / "cpu.weight").read_text().strip())
    except Exception as e:
        log.warning("Failed to read cgroup cpu.weight for %s: %s", container_id[:12], e)
    return None


def build_evidence(
    host_snap: HostSnapshot,
    live: LiveClassifier,
    targets: list[Any],
    trigger_info: dict[str, Any],
    stats_history: list[dict[str, Any]],
) -> dict[str, Any]:
    """Construct evidence dictionary matching the schema expected by the agents."""
    c = live.latest()
    wl_class = c.workload if c else "BUILD"
    confidence = c.confidence if (c and c.confidence is not None) else 1.0


    ctrs_snap = live._ctrs.sample() if hasattr(live, "_ctrs") else None
    ctrs_by_id = {ctr.id: ctr for ctr in (ctrs_snap.containers if ctrs_snap else [])}

    target_dicts = []
    for t in targets:
        c_stat = ctrs_by_id.get(t.container_id)
        target_dicts.append({
            "target_id": t.target_id,
            "container_id": t.container_id,
            "name": t.name,
            "workload": live.workload_of(t.name) or "UNKNOWN",
            "cpu_percent": (c_stat.cpu_percent or 0.0) if c_stat else 0.0,
            "cpu_psi": (c_stat.cpu_psi.some_avg10 if c_stat and c_stat.cpu_psi else 0.0),
            "cpu_weight": t.cpu_weight or 100,
            "cpu_limit_cores": t.cpu_limit_cores,
            "io_write_mib_s": round((c_stat.io_write_bytes_per_s or 0.0) / (2**20), 2) if c_stat else 0.0,
            "oom_kills_delta": (c_stat.memory_oom_kills or 0) if c_stat else 0,
        })


    cpu_psi = host_snap.cpu.psi.some_avg10 if host_snap.cpu.psi else 0.0
    mem_psi = host_snap.memory.psi.some_avg10 if host_snap.memory.psi else 0.0
    io_psi = host_snap.disk.psi.some_avg10 if host_snap.disk.psi else 0.0
    mem_avail_pct = round(100 * host_snap.memory.available_bytes / host_snap.memory.total_bytes, 2)

    return {
        "trigger": trigger_info,
        "workload": {"class": wl_class, "confidence": confidence},
        "host": {
            "cpu_percent": host_snap.cpu.percent,
            "cpu_psi": cpu_psi,
            "mem_avail_pct": mem_avail_pct,
            "mem_psi": mem_psi,
            "io_psi": io_psi,
            "iowait": host_snap.cpu.iowait,
            "cores": host_snap.cpu.core_count,
        },

        "thresholds": {
            "cpu_psi": 15.6,
            "mem_psi": 10.0,
            "mem_avail_min_pct": 10.0,
            "io_psi": 18.4,
        },
        "targets": target_dicts,
        "history": stats_history,
    }


async def run_pipeline_cycle(
    cycle_num: int,
    runner: InMemoryRunner,
    supervisor_agent: Any,
    decision_agent: Any,
    meta_agent: Any,
    handlers: ToolHandlers,
    eval_engine: EvaluationEngine,
    policy_store: PolicyStore,
    stats_engine: StatisticsEngine,
    reference_baseline: float,
    evidence: dict[str, Any],
    log_file: Path,
    docker_client: Any,
    target_source: DockerTargetSource,
    build_container_name: str = "rt-build_vs_indexer-build",
) -> dict[str, Any]:
    """Execute one complete tuning cycle with cache warming and actual before/after measurements."""
    log.info("==================== STARTING VERTICAL SLICE CYCLE %d ====================", cycle_num)
    t0 = time.time()

    # --- STEP 1: Throwaway Warm-up Build (Cache Warming) ---
    cur = get_latest_build(build_container_name, docker_client)
    cur_iter = cur.get("iteration", 0) if cur else 0
    log.info("Phase 1: Running throwaway warm-up build (waiting for iteration > %d)...", cur_iter)
    warm_up_rec = wait_for_build(build_container_name, cur_iter, docker_client, timeout_s=180.0)
    if not warm_up_rec or not warm_up_rec.get("success", False):
        log.error("Warm-up build failed or timed out: %s", warm_up_rec)
        return {"cycle": cycle_num, "status": "failed_warmup", "error": "Warm-up build failed"}

    log.info(
        "Warm-up build completed successfully: iteration=%d, duration=%.2fs (discarded from eval stats)",
        warm_up_rec["iteration"], float(warm_up_rec["duration_s"])
    )
    warm_up_iter = warm_up_rec["iteration"]

    # --- STEP 2: Actual Per-Cycle Before-Measurement ---
    log.info("Phase 2: Taking actual fresh before-measurement build (waiting for iteration > %d)...", warm_up_iter)
    before_rec = wait_for_build(build_container_name, warm_up_iter, docker_client, timeout_s=180.0)
    if not before_rec or not before_rec.get("success", False):
        log.error("Actual before-measurement build failed or timed out: %s", before_rec)
        return {"cycle": cycle_num, "status": "failed_before_measurement", "error": "Before-measurement failed"}

    actual_before_duration = float(before_rec["duration_s"])
    before_iter = before_rec["iteration"]
    log.info(
        "Actual before-measurement: duration=%.2fs (iteration=%d) [ref baseline: %.2fs]",
        actual_before_duration, before_iter, reference_baseline
    )

    # Record initial cgroup state of candidate targets (e.g. indexers)
    targets_before = target_source.list()
    initial_weights = {
        t.target_id: read_container_cpu_weight(t.container_id)
        for t in targets_before if "indexer" in t.name
    }
    log.info("Initial indexer cgroup weights: %s", initial_weights)

    # --- STEP 3: Exploration Rule ---
    force_explore = random.random() < 0.20
    if force_explore:
        evidence["force_explore"] = True
        log.info("Exploration rule triggered: force_explore=True (1-in-5)")

    # --- STEP 4: ADK Supervisor ---
    runner.agent = supervisor_agent
    session = await runner.session_service.create_session(
        app_name="rt-slice",
        user_id="vertical_slice",
        state={"evidence": evidence},
    )

    sup_summary = supervisor_summary(evidence)
    sup_msg = types.Content(
        role="user",
        parts=[types.Part(text="Deterministic trigger summary: " + json.dumps(sup_summary))],
    )

    log.info("Invoking ADK Supervisor...")
    async for _ in runner.run_async(user_id="vertical_slice", session_id=session.id, new_message=sup_msg):
        pass

    session_state = (await runner.session_service.get_session(
        app_name="rt-slice", user_id="vertical_slice", session_id=session.id
    )).state

    delegation = session_state.get("delegation") or {}
    findings = session_state.get("findings") or []
    llm_degraded = bool(session_state.get("llm_degraded"))
    log.info(
        "Supervisor completed: delegated to %s (degraded=%s, reason=%s)",
        delegation.get("resources"), llm_degraded, delegation.get("reason")
    )

    # --- STEP 5: Decision Agent ---
    candidates = actions_for_decision(findings, llm_degraded)
    wl_class = evidence.get("workload", {}).get("class", "BUILD")
    stats_summary = stats_engine.summarize(wl_class, "cpu_contention")
    log.info("Decision stage: %d candidate action(s), %d policy history record(s)", len(candidates), len(stats_summary))

    runner.agent = decision_agent
    dec_session = await runner.session_service.create_session(
        app_name="rt-slice",
        user_id="vertical_slice",
        state={"evidence": evidence, "llm_degraded": llm_degraded},
    )
    dec_payload = decision_summary(evidence, candidates, stats_summary)
    dec_msg = types.Content(
        role="user",
        parts=[types.Part(text="Decision input: " + json.dumps(dec_payload))],
    )

    log.info("Invoking ADK Decision Agent...")
    dec_text = ""
    async for ev in runner.run_async(user_id="vertical_slice", session_id=dec_session.id, new_message=dec_msg):
        if ev.is_final_response() and ev.content and ev.content.parts:
            dec_text = "".join(p.text or "" for p in ev.content.parts)

    decision_out = parse_decision_output(dec_text)
    log.info(
        "Decision Agent verdict: %s | Reasoning: %s",
        decision_out.chosen_action, decision_out.reasoning
    )

    # --- STEP 6: Execute Decision via ToolHandlers (Safety Gate + Controller) ---
    exec_result = execute_decision(decision_out, handlers, llm_degraded=llm_degraded)
    action_applied = exec_result.get("status") == "applied"
    action_id = exec_result.get("action_id")
    log.info("Action execution result: %s (action_id=%s)", exec_result.get("status"), action_id)

    # Independent post-action cgroup verification
    applied_weights = {
        t.target_id: read_container_cpu_weight(t.container_id)
        for t in target_source.list() if "indexer" in t.name
    }
    log.info("Post-action indexer cgroup weights: %s", applied_weights)

    # --- STEP 7: Actual Per-Cycle After-Measurement ---
    eval_result: EvaluationResult | None = None
    meta_decision: Any = None
    after_rec: dict[str, Any] | None = None

    if action_applied and action_id:
        log.info("Phase 7: Taking actual after-measurement build (waiting for iteration > %d)...", before_iter)
        after_rec = wait_for_build(build_container_name, before_iter, docker_client, timeout_s=180.0)

        if not after_rec or not after_rec.get("success", False):
            log.error("After-measurement build failed or timed out: %s", after_rec)
            # Trigger rollback due to failed measurement / timeout
            eval_result = eval_engine.evaluate_discrete(
                action_id=action_id,
                before_duration=actual_before_duration,
                after_duration=None,
                action_timestamp=time.time(),
                reference_baseline=reference_baseline,
            )
        else:
            actual_after_duration = float(after_rec["duration_s"])
            after_iter = after_rec["iteration"]
            log.info(
                "Actual after-measurement: duration=%.2fs (iteration=%d) vs before %.2fs",
                actual_after_duration, after_iter, actual_before_duration
            )

            # Evaluate actual after vs actual before duration
            eval_result = eval_engine.evaluate_discrete(
                action_id=action_id,
                before_duration=actual_before_duration,
                after_duration=actual_after_duration,
                reference_baseline=reference_baseline,
            )

        log.info(
            "Evaluation Verdict: %s (improvement=%.2f%%, detail=%s, rolled_back=%s)",
            eval_result.verdict, eval_result.improvement_pct or 0.0,
            eval_result.detail, eval_result.rolled_back
        )

        # Independent verification of final cgroup state after rollback/keep
        final_weights = {
            t.target_id: read_container_cpu_weight(t.container_id)
            for t in target_source.list() if "indexer" in t.name
        }
        log.info("Final indexer cgroup weights after evaluation resolution: %s", final_weights)

        # --- STEP 8: Record in PolicyStore ---
        action = decision_out.chosen_action
        if action:
            policy_rec = policy_store.record_outcome(
                workload=wl_class,
                condition="cpu_contention",
                action_tool=action.tool,
                action_target=action.target_id,
                action_param={"weight": action.weight} if action.weight is not None else {"cpus": action.cpus},
                outcome=eval_result.verdict,
                improvement_value=eval_result.improvement_pct,
            )
            log.info(
                "PolicyStore record: sample_count=%d, success_count=%d, failure_count=%d, avg_improvement=%.2f, last_outcome=%s",
                policy_rec["sample_count"], policy_rec["success_count"], policy_rec["failure_count"],
                policy_rec["avg_improvement"] or 0.0, policy_rec["last_outcome"]
            )

            # --- STEP 9: Meta-Agent Policy Governance ---
            call_llm, skip_reason = evaluate_policy_candidate(policy_rec, llm_degraded=llm_degraded)
            if call_llm:
                log.info("Invoking ADK Meta-Agent for policy governance (eligible candidate or demotion reasoning)...")
                runner.agent = meta_agent
                meta_session = await runner.session_service.create_session(
                    app_name="rt-slice",
                    user_id="vertical_slice",
                )
                meta_msg = types.Content(
                    role="user",
                    parts=[types.Part(text="Evaluate policy promotion: " + json.dumps(policy_rec))],
                )
                meta_text = ""
                async for ev in runner.run_async(user_id="vertical_slice", session_id=meta_session.id, new_message=meta_msg):
                    if ev.is_final_response() and ev.content and ev.content.parts:
                        meta_text = "".join(p.text or "" for p in ev.content.parts)
                meta_decision = parse_meta_decision(meta_text)
                log.info("Meta-Agent decision: promote=%s | Reason: %s", meta_decision.promote, meta_decision.reasoning)
                if meta_decision.promote:
                    policy_store.set_promoted(
                        workload=wl_class,
                        condition="cpu_contention",
                        action_tool=action.tool,
                        action_target=action.target_id,
                        action_param={"weight": action.weight} if action.weight is not None else {"cpus": action.cpus},
                        promoted=True,
                    )
                    log.info("POLICY PROMOTED TO DEFAULT STORE!")
            else:
                log.info("Meta-Agent skipped: %s", skip_reason)
                meta_decision = {"promote": False, "reasoning": skip_reason}

    cycle_log = {
        "cycle": cycle_num,
        "timestamp": t0,
        "duration_s": round(time.time() - t0, 2),
        "trigger": evidence.get("trigger"),
        "reference_baseline_s": reference_baseline,
        "warm_up_build": warm_up_rec,
        "before_build": before_rec,
        "after_build": after_rec,
        "indexer_initial_weights": initial_weights,
        "indexer_post_action_weights": applied_weights,
        "force_explore": force_explore,
        "delegation": delegation,
        "candidates": candidates,
        "decision": decision_out.model_dump() if decision_out else None,
        "execution": exec_result,
        "evaluation": eval_result.to_dict() if eval_result else None,
        "meta": meta_decision if isinstance(meta_decision, dict) else (meta_decision.model_dump() if meta_decision else None),
        "llm_provenance": session_state.get("llm_provenance"),
        "llm_degraded": llm_degraded,
    }

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(cycle_log) + "\n")

    return cycle_log


async def main():
    parser = argparse.ArgumentParser(description="ResourceTuner Phase 2 Milestone: Vertical Slice")
    parser.add_argument("--cycles", type=int, default=5, help="Number of tuning cycles to run (default: 5)")
    parser.add_argument("--scenario", default="build_vs_indexer", help="Workload scenario name (default: build_vs_indexer)")
    parser.add_argument("--level", default="high", help="Workload intensity level (default: high)")
    parser.add_argument("--baseline-duration", type=float, default=37.38, help="Reference warm-cache baseline duration in seconds")
    parser.add_argument("--cooldown-s", type=float, default=20.0, help="Cooldown seconds between cycles")
    parser.add_argument("--log-file", type=Path, default=REPO / "data" / "vertical_slice_log.jsonl")
    parser.add_argument("--no-start-scenario", action="store_true", help="Assume scenario containers are already running")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config()
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    docker_client = docker.from_env(timeout=30)

    log.info("Starting ResourceTuner Vertical Slice Milestone (%d cycles)", args.cycles)
    log.info("Reference baseline benchmark: %.2fs | Cooldown: %.1fs", args.baseline_duration, args.cooldown_s)

    # 1. Start workload scenario if not disabled
    if not args.no_start_scenario:
        log.info("Starting scenario %s (level=%s)...", args.scenario, args.level)
        os.system(f"./venv/bin/python scripts/run_scenario.py start {args.scenario} --level {args.level}")
        time.sleep(5.0)

    # 2. Setup Background Classifier & Telemetry
    telemetry = HostTelemetry(top_n=25)
    live = LiveClassifier().start()
    log.info("Waiting for LiveClassifier to accumulate window...")
    live.wait_ready(timeout_s=15.0)

    # 3. Setup Stores, Controller, Gate, Handlers
    action_store = ActionStore(REPO / "data" / "actions.db")
    controller = ResourceController(action_store)
    gate = SafetyGate(SafetyConfig.load(), ControllerGateContext(action_store, live))
    target_source = DockerTargetSource()
    handlers = ToolHandlers(target_source, controller, gate=gate)

    policy_store = PolicyStore(REPO / "data" / "policy.db")
    stats_engine = StatisticsEngine(policy_store)
    eval_engine = EvaluationEngine(controller=controller)

    # 4. Setup Agents and ADK Runner
    sup_model_name = gemini_model_for(cfg, "supervisor")
    dec_model_name = gemini_model_for(cfg, "decision")
    meta_model_name = gemini_model_for(cfg, "meta")

    diagnose_fn = make_crew_diagnose()
    supervisor_agent = build_supervisor(sup_model_name, diagnose_fn)
    decision_agent = build_decision_agent(dec_model_name, safety_cfg=gate.config)
    meta_agent = build_meta_agent(meta_model_name)

    plugins = [ModelFallbackPlugin(cfg), DegradedActionBlocker()]
    runner = InMemoryRunner(agent=supervisor_agent, app_name="rt-slice", plugins=plugins)

    # 5. Main Execution Loop
    psi_history: deque[float] = deque(maxlen=10)
    cycle_count = 0

    try:
        while cycle_count < args.cycles:
            # Sample live host telemetry at 1 Hz
            snap = telemetry.sample()
            cpu_psi = snap.cpu.psi.some_avg10 if snap.cpu.psi else 0.0
            psi_history.append(cpu_psi)

            # Check primary trigger: cpu_psi >= 15.6% for 10 consecutive seconds
            if len(psi_history) == 10 and all(p >= 15.6 for p in psi_history):
                log.info(
                    "TRIGGER DETECTED: cpu_psi >= 15.6%% for 10s (latest=%.1f%%)",
                    cpu_psi
                )
                trigger_info = {
                    "resource": "cpu",
                    "metric": "host.cpu_psi",
                    "value": cpu_psi,
                    "threshold": 15.6,
                    "persisted_s": 10,
                }

                targets = target_source.list()
                history = policy_store.get_history("BUILD", "cpu_contention", top_n=3)
                evidence = build_evidence(snap, live, targets, trigger_info, history)

                cycle_count += 1
                await run_pipeline_cycle(
                    cycle_num=cycle_count,
                    runner=runner,
                    supervisor_agent=supervisor_agent,
                    decision_agent=decision_agent,
                    meta_agent=meta_agent,
                    handlers=handlers,
                    eval_engine=eval_engine,
                    policy_store=policy_store,
                    stats_engine=stats_engine,
                    reference_baseline=args.baseline_duration,
                    evidence=evidence,
                    log_file=args.log_file,
                    docker_client=docker_client,
                    target_source=target_source,
                    build_container_name="rt-build_vs_indexer-build",
                )

                psi_history.clear()
                if cycle_count < args.cycles:
                    log.info("Entering cooldown (%ds)...", int(args.cooldown_s))
                    time.sleep(args.cooldown_s)
            else:
                time.sleep(1.0)

        log.info("ALL %d CYCLES COMPLETED SUCCESSFULLY!", args.cycles)

    finally:
        live.stop()
        if not args.no_start_scenario:
            log.info("Stopping scenario containers...")
            os.system("./venv/bin/python scripts/run_scenario.py stop")


if __name__ == "__main__":
    asyncio.run(main())
