"""Tests for Step 9 components: PolicyStore, StatisticsEngine, Decision Agent, EvaluationEngine, and Meta-Agent."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from resourcetuner.agents.adk.decision import (
    ChosenAction,
    DecisionOutput,
    build_decision_agent,
    decision_summary,
    execute_decision,
    parse_decision_output,
)
from resourcetuner.agents.adk.meta import (
    MetaDecision,
    build_meta_agent,
    evaluate_policy_candidate,
    parse_meta_decision,
)
from resourcetuner.evaluation.engine import EvaluationEngine, EvaluationResult
from resourcetuner.mcp_server.tools import Target, ToolHandlers, ProposedAction
from resourcetuner.policy.store import PolicyStore
from resourcetuner.safety.gate import SafetyConfig, SafetyGate, StaticContext
from resourcetuner.stats.statistics_engine import StatisticsEngine


# --- PolicyStore Tests ---

def test_policy_store_record_and_ema(tmp_path: Path):
    store = PolicyStore(tmp_path / "policy.db")
    # 1st outcome: IMPROVED by 10.0
    r1 = store.record_outcome(
        workload="BUILD",
        condition="cpu_contention",
        action_tool="set_container_cpu_weight",
        action_target="container:rt-indexer",
        action_param={"weight": 50},
        outcome="IMPROVED",
        improvement_value=10.0,
    )
    assert r1["success_count"] == 1
    assert r1["failure_count"] == 0
    assert r1["sample_count"] == 1
    assert r1["avg_improvement"] == 10.0
    assert r1["last_outcome"] == "IMPROVED"

    # 2nd outcome: IMPROVED by 20.0 -> EMA: 0.3 * 20 + 0.7 * 10 = 13.0
    r2 = store.record_outcome(
        workload="BUILD",
        condition="cpu_contention",
        action_tool="set_container_cpu_weight",
        action_target="container:rt-indexer",
        action_param={"weight": 50},
        outcome="IMPROVED",
        improvement_value=20.0,
    )
    assert r2["success_count"] == 2
    assert r2["failure_count"] == 0
    assert r2["sample_count"] == 2
    assert r2["avg_improvement"] == 13.0


def test_policy_store_relevance_and_promotion(tmp_path: Path):
    store = PolicyStore(tmp_path / "policy.db")
    store.record_outcome("BUILD", "cpu_contention", "tool1", "t1", {"w": 1}, "IMPROVED", 15.0)
    store.record_outcome("TESTING", "cpu_contention", "tool2", "t2", {"w": 2}, "IMPROVED", 5.0)
    store.record_outcome("BUILD", "ram_pressure", "tool3", "t3", {"w": 3}, "DEGRADED", -5.0)

    history = store.get_history("BUILD", "cpu_contention", top_n=3)
    assert len(history) == 3
    assert history[0]["workload"] == "BUILD" and history[0]["condition"] == "cpu_contention"

    assert store.set_promoted("BUILD", "cpu_contention", "tool1", "t1", {"w": 1}, True)
    pref = store.preferred_action("BUILD", "cpu_contention")
    assert pref is not None
    assert pref["action_tool"] == "tool1"
    assert pref["promoted"] is True


def test_policy_store_preferred_action_floor(tmp_path: Path):
    store = PolicyStore(tmp_path / "policy.db")
    for _ in range(9):
        store.record_outcome("BUILD", "cpu_contention", "tool1", "t1", {"w": 1}, "IMPROVED", 10.0)
    assert store.preferred_action("BUILD", "cpu_contention") is None

    store.record_outcome("BUILD", "cpu_contention", "tool1", "t1", {"w": 1}, "IMPROVED", 10.0)
    pref = store.preferred_action("BUILD", "cpu_contention")
    assert pref is not None
    assert pref["action_tool"] == "tool1"
    assert pref["sample_count"] == 10


# --- StatisticsEngine Tests ---

def test_statistics_engine_summarize(tmp_path: Path):
    store = PolicyStore(tmp_path / "policy.db")
    for _ in range(3):
        store.record_outcome("BUILD", "cpu_contention", "set_container_cpu_weight", "rt-idx", {"weight": 50}, "IMPROVED", 12.0)
    store.record_outcome("BUILD", "cpu_contention", "set_container_cpu_weight", "rt-idx", {"weight": 50}, "DEGRADED", -8.0)

    stats = StatisticsEngine(store)
    summary = stats.summarize("BUILD", "cpu_contention", top_n=3)
    assert len(summary) == 1
    key = list(summary.keys())[0]
    entry = summary[key]
    assert entry["sample_count"] == 4
    assert entry["success_rate"] == 0.75
    assert entry["last_outcome"] == "DEGRADED"
    assert entry["recent_trend"] == "degrading"


def test_policy_store_records_an_append_only_outcome_per_call(tmp_path: Path):
    """Reported finding: `policies` only ever held lifetime aggregates, so `recent_trend` had
    no per-event history to compare against. `outcomes` (schema.sql) is the fix — one row per
    record_outcome() call, oldest first."""
    store = PolicyStore(tmp_path / "policy.db")
    store.record_outcome("BUILD", "cpu_contention", "tool1", "t1", {"w": 1}, "IMPROVED", 10.0)
    store.record_outcome("BUILD", "cpu_contention", "tool1", "t1", {"w": 1}, "DEGRADED", -3.0)

    rows = store.get_outcomes("BUILD", "cpu_contention", "tool1", "t1", {"w": 1})
    assert [r["outcome"] for r in rows] == ["IMPROVED", "DEGRADED"]       # oldest first
    assert rows[0]["improvement_value"] == 10.0 and rows[1]["improvement_value"] == -3.0
    assert rows[0]["ts"] <= rows[1]["ts"]

    # A different policy key must not see these rows.
    assert store.get_outcomes("TESTING", "cpu_contention", "tool1", "t1", {"w": 1}) == []


def test_recent_trend_is_a_real_recent_vs_older_comparison_not_last_outcome_alone(tmp_path: Path):
    """The specific gap this finding reported: a single bad last_outcome after a long run of
    good ones must not alone flip the verdict, and a genuine improving run must read as
    improving even though its oldest outcomes were poor — `last_outcome` alone could not tell
    these two histories apart, but a real recent-half vs older-half split can."""
    stats_mod = __import__("resourcetuner.stats.statistics_engine", fromlist=["_recent_vs_older_trend"])

    # Older half all good, recent half all bad -> degrading (even though overall success rate
    # 50% would read as "stable" under the old success_rate>=0.70 / <0.40 heuristic).
    outcomes = ([{"outcome": "IMPROVED", "improvement_value": 10.0, "ts": i} for i in range(4)]
                + [{"outcome": "DEGRADED", "improvement_value": -5.0, "ts": 4 + i} for i in range(4)])
    assert stats_mod._recent_vs_older_trend(outcomes) == "degrading"

    # Older half all bad, recent half all good -> improving.
    outcomes_rev = list(reversed([dict(o, ts=7 - i) for i, o in enumerate(outcomes)]))
    assert stats_mod._recent_vs_older_trend(outcomes_rev) == "improving"

    # Equal success rate in both halves, and improvement roughly flat -> stable.
    flat = [{"outcome": "IMPROVED", "improvement_value": 5.0, "ts": i} for i in range(2)] + \
           [{"outcome": "DEGRADED", "improvement_value": -5.0, "ts": 2 + i} for i in range(2)] + \
           [{"outcome": "IMPROVED", "improvement_value": 5.0, "ts": 4 + i} for i in range(2)] + \
           [{"outcome": "DEGRADED", "improvement_value": -5.0, "ts": 6 + i} for i in range(2)]
    assert stats_mod._recent_vs_older_trend(flat) == "stable"

    # Too few granular outcomes to split meaningfully -> defer to the caller's fallback.
    assert stats_mod._recent_vs_older_trend([{"outcome": "IMPROVED", "improvement_value": 1.0, "ts": 0}] * 3) is None


def test_summarize_falls_back_to_the_heuristic_for_a_policy_with_no_granular_outcomes(tmp_path: Path):
    """A policy entry whose aggregate row predates the `outcomes` table (or simply has too few
    recorded outcomes) has nothing to split — summarize() must say so honestly via the existing
    coarser heuristic, not silently invent a recent-vs-older split from data that is not there."""
    store = PolicyStore(tmp_path / "policy.db")
    store.record_outcome("BUILD", "cpu_contention", "tool1", "t1", {"w": 1}, "IMPROVED", 10.0)
    # Manually simulate a pre-`outcomes`-table row: aggregate exists, no outcomes rows for it.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DELETE FROM outcomes")
        conn.commit()

    stats = StatisticsEngine(store)
    entry = next(iter(stats.summarize("BUILD", "cpu_contention", top_n=1).values()))
    assert entry["sample_count"] == 1
    assert entry["recent_trend"] == "insufficient_data"   # sample_count < 2 path, unaffected

    # Now push it past 2 samples with still-sparse (but present) granular outcomes (< 4 total):
    # the real split needs >= 4, so this must still fall back to the heuristic, not error out.
    store.record_outcome("BUILD", "cpu_contention", "tool1", "t1", {"w": 1}, "IMPROVED", 8.0)
    entry = next(iter(stats.summarize("BUILD", "cpu_contention", top_n=1).values()))
    assert entry["sample_count"] == 2
    assert entry["recent_trend"] in ("improving", "stable", "degrading")  # heuristic path, no crash


# --- Decision Agent Tests ---

def test_decision_output_parser():
    raw = '{"chosen_action": {"tool": "set_container_cpu_weight", "target_id": "container:rt-indexer", "weight": 50}, "reasoning": "CPU PSI is 24% and indexer uses 45% CPU."}'
    parsed = parse_decision_output(raw)
    assert parsed.chosen_action is not None
    assert parsed.chosen_action.tool == "set_container_cpu_weight"
    assert parsed.chosen_action.weight == 50
    assert "CPU PSI is 24%" in parsed.reasoning

    fenced = f"```json\n{raw}\n```"
    parsed_fenced = parse_decision_output(fenced)
    assert parsed_fenced.chosen_action is not None

    raw_none = '{"chosen_action": null, "reasoning": "No safe action found."}'
    parsed_none = parse_decision_output(raw_none)
    assert parsed_none.chosen_action is None

    bad = "I think we should tune container 1"
    parsed_bad = parse_decision_output(bad)
    assert parsed_bad.chosen_action is None


def test_execute_decision_guards():
    mock_handlers = MagicMock(spec=ToolHandlers)

    dec = DecisionOutput(
        chosen_action=ChosenAction(tool="set_container_cpu_weight", target_id="c1", weight=50),
        reasoning="Test",
    )
    # Degraded mode blocks execution
    res_degraded = execute_decision(dec, mock_handlers, llm_degraded=True)
    assert res_degraded["status"] == "blocked"
    assert res_degraded["error"] == "llm_degraded"
    mock_handlers.set_container_cpu_weight.assert_not_called()

    # Valid execution passes source_degraded=False
    execute_decision(dec, mock_handlers, llm_degraded=False)
    mock_handlers.set_container_cpu_weight.assert_called_once_with("c1", 50, source_degraded=False)


# --- EvaluationEngine Tests & Boundary Checks ---

def test_discrete_threshold_exact_boundaries():
    """Verify exact +-10% discrete thresholds (improved <= 0.90, degraded >= 1.10)."""
    engine = EvaluationEngine(controller=None)
    before = 100.0

    # 1. Exactly -10% (90.0) -> IMPROVED
    r_imp = engine.evaluate_discrete("act", before_duration=before, after_duration=90.0)
    assert r_imp.verdict == "IMPROVED"

    # 2. -9.9% (90.1) -> UNCHANGED (inside noise band)
    r_unch1 = engine.evaluate_discrete("act", before_duration=before, after_duration=90.1)
    assert r_unch1.verdict == "UNCHANGED"

    # 3. +9.9% (109.9) -> UNCHANGED (inside noise band)
    r_unch2 = engine.evaluate_discrete("act", before_duration=before, after_duration=109.9)
    assert r_unch2.verdict == "UNCHANGED"

    # 4. Exactly +10% (110.0) -> DEGRADED
    r_deg = engine.evaluate_discrete("act", before_duration=before, after_duration=110.0)
    assert r_deg.verdict == "DEGRADED"


def test_continuous_threshold_exact_boundaries():
    """Verify exact +-15% continuous thresholds (improved <= 0.85, degraded >= 1.15)."""
    engine = EvaluationEngine(controller=None)
    pre_psi = 100.0

    # 1. Exactly -15% (85.0) -> IMPROVED
    r_imp = engine.evaluate_continuous("act", pre_psi=pre_psi, post_psi=85.0)
    assert r_imp.verdict == "IMPROVED"

    # 2. -14.9% (85.1) -> UNCHANGED
    r_unch1 = engine.evaluate_continuous("act", pre_psi=pre_psi, post_psi=85.1)
    assert r_unch1.verdict == "UNCHANGED"

    # 3. +14.9% (114.9) -> UNCHANGED
    r_unch2 = engine.evaluate_continuous("act", pre_psi=pre_psi, post_psi=114.9)
    assert r_unch2.verdict == "UNCHANGED"

    # 4. Exactly +15% (115.0) -> DEGRADED
    r_deg = engine.evaluate_continuous("act", pre_psi=pre_psi, post_psi=115.0)
    assert r_deg.verdict == "DEGRADED"


def test_failed_build_and_timeout_trigger_rollback():
    """Verify that build failure inside container or timeout triggers rollback, never artificial improvement."""
    mock_controller = MagicMock()
    mock_controller.rollback.return_value = {"status": "rolled_back"}
    mock_controller.store.get.return_value = {"action_id": "act-fail", "status": "rolled_back"}

    engine = EvaluationEngine(controller=mock_controller)

    # 1. Failed build inside container (success=False)
    mock_docker = MagicMock()
    mock_container = MagicMock()
    mock_container.logs.return_value = b'{"event":"build","iteration":1,"duration_s":12.0,"success":false}\n'
    mock_docker.containers.get.return_value = mock_container

    r_fail = engine.evaluate_discrete(
        "act-fail", before_duration=40.0, action_timestamp=100.0, timeout_s=5.0, docker_client=mock_docker
    )
    assert r_fail.verdict == "ERROR"
    assert r_fail.rolled_back is True
    assert "failed" in r_fail.detail
    mock_controller.rollback.assert_called_with("act-fail")
    assert r_fail.final_controller_state["status"] == "rolled_back"

    # 2. Timeout (no log lines)
    mock_container.logs.return_value = b''
    r_timeout = engine.evaluate_discrete(
        "act-timeout", before_duration=40.0, action_timestamp=100.0, timeout_s=0.1, docker_client=mock_docker
    )
    assert r_timeout.verdict == "TIMEOUT"
    assert r_timeout.rolled_back is True
    mock_controller.rollback.assert_called_with("act-timeout")


# --- Meta-Agent Boundaries Tests ---

def test_meta_agent_sample_count_and_rate_boundaries():
    """Verify sample counts 4, 5, 9, 10 and success rate 80% boundary."""
    # sample_count = 4 -> skip LLM
    call, r = evaluate_policy_candidate({"sample_count": 4, "success_rate": 1.0, "avg_improvement": 10.0})
    assert call is False
    assert "sample_count=4 < 10" in r

    # sample_count = 5 -> skip LLM (no early promotion below 10)
    call, r = evaluate_policy_candidate({"sample_count": 5, "success_rate": 1.0, "avg_improvement": 10.0})
    assert call is False
    assert "sample_count=5 < 10" in r

    # sample_count = 9 -> skip LLM
    call, r = evaluate_policy_candidate({"sample_count": 9, "success_rate": 1.0, "avg_improvement": 10.0})
    assert call is False
    assert "sample_count=9 < 10" in r

    # sample_count = 10, success_rate = 0.79 (79%) -> skip LLM (< 80%)
    call, r = evaluate_policy_candidate({"sample_count": 10, "success_rate": 0.79, "avg_improvement": 10.0})
    assert call is False
    assert "success_rate=0.79 < 0.80" in r

    # sample_count = 10, success_rate = 0.80, avg_imp = 5.0 -> call LLM for promotion reasoning
    call, r = evaluate_policy_candidate({"sample_count": 10, "success_rate": 0.80, "avg_improvement": 5.0})
    assert call is True
    assert r is None

    # sample_count = 10, success_rate = 0.85, avg_imp = -1.0 -> skip LLM (not positive)
    call, r = evaluate_policy_candidate({"sample_count": 10, "success_rate": 0.85, "avg_improvement": -1.0})
    assert call is False
    assert "avg_improvement=-1.0 <= 0" in r

    # sample_count = 10, success_rate = 0.85, llm_degraded = True -> strictly blocked
    call, r = evaluate_policy_candidate({"sample_count": 10, "success_rate": 0.85, "avg_improvement": 5.0}, llm_degraded=True)
    assert call is False
    assert "Degraded LLM mode" in r

    # sample_count = 7, last_outcome = DEGRADED -> call LLM for demotion evaluation
    call, r = evaluate_policy_candidate({"sample_count": 7, "success_rate": 0.7, "last_outcome": "DEGRADED"})
    assert call is True
    assert r is None


def test_meta_module_docstring_states_the_real_sample_floor():
    """The module docstring once said '< 5' while the code (and the LLM's own instruction)
    always used 10 — reconciled to '< 10' so the documented floor can never silently diverge
    from `evaluate_policy_candidate`'s actual boundary again."""
    import resourcetuner.agents.adk.meta as meta_module
    assert "(< 5)" not in meta_module.__doc__     # the stale floor this finding reported
    assert "(< 10)" in meta_module.__doc__
    assert "sample_count >= 10" in meta_module.INSTRUCTION


def test_force_explore_is_recorded_but_not_yet_consumed():
    """Documents a deliberate deferral (brief §7's 1-in-5 exploration rule), not a silent gap.

    `force_explore` is rolled and written to the cycle log and to the evidence dict passed into
    the pipeline, but nothing downstream reads it: `project_evidence()` only ever forwards
    `ALWAYS` keys plus per-dimension host/target/threshold projections, and `decision_summary()`
    builds the Decision Agent's input from exactly 4 fixed keys. Implementing the flag's real
    effect would mean guessing, from this code alone, what the original brief intended
    "exploration" to change about agent behaviour — which this project will not do silently.

    If a future change wires `force_explore` into an agent, this test should be rewritten to
    assert that new behaviour, not deleted quietly."""
    from resourcetuner.agents.adk.decision import decision_summary
    from resourcetuner.agents.diagnostic_crew.evidence import project_evidence

    evidence = {
        "trigger": {"metric": "cpu_psi", "value": 20}, "workload": {"class": "BUILD"},
        "host": {"cpu_percent": 90, "cores": 6, "load_1m": 2, "cpu_psi": 20},
        "targets": [], "force_explore": True,
    }

    for agent in ("cpu", "ram", "io"):
        assert "force_explore" not in project_evidence(agent, evidence)

    summary = decision_summary(evidence, candidates=[], stats_summary={})
    assert "force_explore" not in summary
    assert set(summary) == {"workload", "trigger", "candidate_actions", "history_statistics"}


def test_tool_handlers_and_gate_reject_degraded_source():
    """Verify that ToolHandlers passes source_degraded and the Safety Gate rejects it."""
    mock_actuator = MagicMock()
    mock_target = Target(
        target_id="container:rt-indexer",
        container_id="c" * 64,
        name="rt-indexer",
        image="resourcetuner/indexer-sim",
        state="running",
        cpu_weight=100,
        io_weight=100,
        cpu_limit_cores=None,
        memory_max_bytes=None,
    )
    mock_sources = MagicMock()
    mock_sources.list.return_value = [mock_target]

    gate = SafetyGate(SafetyConfig.load(), StaticContext(workloads={"rt-indexer": "CODE_INDEXING"}, host="BUILD"))
    handlers = ToolHandlers(mock_sources, mock_actuator, gate=gate)

    # Calling with source_degraded=True must be REJECTED by the Safety Gate
    res = handlers.set_container_cpu_weight("container:rt-indexer", 50, source_degraded=True)
    assert res["status"] == "rejected"
    assert any("degraded_source" in r for r in res["reasons"])
    mock_actuator.apply.assert_not_called()



    # Calling with source_degraded=False is APPROVED
    res_ok = handlers.set_container_cpu_weight("container:rt-indexer", 50, source_degraded=False)
    assert res_ok.get("status") != "rejected"


def test_meta_decision_parser():
    raw = '{"promote": true, "reasoning": "Policy demonstrated 90% success across 10 samples with 12s average improvement."}'
    parsed = parse_meta_decision(raw)
    assert parsed.promote is True
    assert "90% success" in parsed.reasoning


def test_end_to_end_pipeline_offline(tmp_path: Path):
    import asyncio
    from google.adk.runners import InMemoryRunner
    from google.adk.models import BaseLlm, LlmResponse
    from google.genai import types

    class ScriptedLlm(BaseLlm):
        model: str = "scripted"
        script: list = []

        @classmethod
        def supported_models(cls):
            return ["scripted"]

        async def generate_content_async(self, llm_request, stream=False):
            yield self.script.pop(0)

    async def _run():
        policy_store = PolicyStore(tmp_path / "policy.db")
        stats_engine = StatisticsEngine(policy_store)

        mock_actuator = MagicMock()
        mock_actuator.name = "mock_actuator"
        mock_actuator.apply.return_value = {"status": "applied"}

        mock_target = MagicMock()
        mock_target.target_id = "container:rt-indexer"
        mock_target.name = "rt-indexer"
        mock_target.state = "running"
        mock_target.cpu_weight = 100
        mock_target.container_id = "cid1"

        mock_sources = MagicMock()
        mock_sources.list.return_value = [mock_target]

        gate = MagicMock()
        verdict = MagicMock()
        verdict.approved = True
        verdict.reasons = []
        gate.evaluate.return_value = verdict

        handlers = ToolHandlers(actuator=mock_actuator, targets=mock_sources, gate=gate)

        decision_model = ScriptedLlm(script=[
            LlmResponse(content=types.Content(role="model", parts=[
                types.Part(text='{"chosen_action": {"tool": "set_container_cpu_weight", "target_id": "container:rt-indexer", "weight": 50}, "reasoning": "Indexer uses 45% CPU, lowering weight."}')
            ]))
        ])
        decision_agent = build_decision_agent(decision_model)
        runner = InMemoryRunner(agent=decision_agent, app_name="rt-test")
        session = await runner.session_service.create_session(app_name="rt-test", user_id="u")

        evidence = {"workload": {"class": "BUILD"}, "trigger": "cpu_psi >= 15.6%"}
        candidates = [{"tool": "set_container_cpu_weight", "target_id": "container:rt-indexer", "weight": 50}]
        stats_summary = stats_engine.summarize("BUILD", "cpu_contention")
        prompt_payload = decision_summary(evidence, candidates, stats_summary)

        msg = types.Content(role="user", parts=[types.Part(text=json.dumps(prompt_payload))])
        final_text = ""
        async for ev in runner.run_async(user_id="u", session_id=session.id, new_message=msg):
            if ev.is_final_response() and ev.content and ev.content.parts:
                final_text = "".join(p.text or "" for p in ev.content.parts)

        decision_out = parse_decision_output(final_text)
        assert decision_out.chosen_action is not None
        assert decision_out.chosen_action.weight == 50

        exec_res = execute_decision(decision_out, handlers)
        assert exec_res["status"] == "applied"
        mock_actuator.apply.assert_called_once()

        eval_engine = EvaluationEngine(controller=None)
        eval_res = eval_engine.evaluate_discrete(
            action_id="act-e2e",
            before_duration=40.0,
            after_duration=32.0,
        )
        assert eval_res.verdict == "IMPROVED"
        assert eval_res.measured_value == 32.0

        recorded = policy_store.record_outcome(
            workload="BUILD",
            condition="cpu_contention",
            action_tool="set_container_cpu_weight",
            action_target="container:rt-indexer",
            action_param={"weight": 50},
            outcome=eval_res.verdict,
            improvement_value=eval_res.improvement_pct,
        )
        assert recorded["success_count"] == 1
        assert recorded["sample_count"] == 1

        call_llm, skip_reason = evaluate_policy_candidate(recorded)
        assert call_llm is False
        assert "sample_count=1 < 10" in skip_reason

    asyncio.run(_run())


# --- Decision Agent Bounds & Safety Gate Enforcement Tests ---

def test_decision_agent_bounds_in_instruction():
    """Ensure Decision Agent prompt derives bounds from SafetyConfig."""
    cfg = SafetyConfig.load()
    agent = build_decision_agent("test-model", safety_cfg=cfg)
    instr = agent.instruction
    assert "cpu.weight" in instr
    assert "minimum 10 (floor)" in instr
    assert "maximum 1000 (ceiling)" in instr
    assert "5x jump limit" in instr
    assert "cpu.max" in instr
    assert "0.5 cores" in instr
    assert "no_action" in instr
    assert "DO NOT propose an out-of-bounds action" in instr


def test_decision_output_parser_no_action_formats():
    """Parser must handle null, 'no_action' strings, and no_action tools gracefully."""
    # 1. chosen_action: null
    r1 = parse_decision_output('{"chosen_action": null, "reasoning": "No candidate fits within bounds."}')
    assert r1.chosen_action is None
    assert "No candidate" in r1.reasoning

    # 2. chosen_action: "no_action"
    r2 = parse_decision_output('{"chosen_action": "no_action", "reasoning": "Proposed weight 5 is below floor 10."}')
    assert r2.chosen_action is None
    assert "below floor 10" in r2.reasoning

    # 3. chosen_action tool: "no_action"
    r3 = parse_decision_output('{"chosen_action": {"tool": "no_action", "target_id": "none"}, "reasoning": "Jump exceeds 5x."}')
    assert r3.chosen_action is None
    assert "Jump exceeds 5x" in r3.reasoning


def test_decision_agent_respects_floor_and_gate_enforces():
    """When a candidate violates floor (weight < 10), Decision Agent rejects it (no_action).
    If an adversarial agent proposes weight < 10 anyway, Safety Gate authoritatively blocks it.
    """
    import asyncio
    from google.adk.runners import InMemoryRunner
    from google.adk.models import BaseLlm, LlmResponse
    from google.genai import types

    class ScriptedLlm(BaseLlm):
        model: str = "scripted"
        script: list = []

        @classmethod
        def supported_models(cls):
            return ["scripted"]

        async def generate_content_async(self, llm_request, stream=False):
            yield self.script.pop(0)

    # 1. Decision agent obeys bounds and returns no_action
    model = ScriptedLlm(script=[
        LlmResponse(content=types.Content(role="model", parts=[
            types.Part(text='{"chosen_action": "no_action", "reasoning": "Candidate proposed weight 5, which violates the minimum floor of 10."}')
        ]))
    ])
    agent = build_decision_agent(model)

    async def _test():
        runner = InMemoryRunner(agent=agent, app_name="rt-test")
        session = await runner.session_service.create_session(app_name="rt-test", user_id="u")
        msg = types.Content(role="user", parts=[types.Part(text="{}")])
        final_text = ""
        async for ev in runner.run_async(user_id="u", session_id=session.id, new_message=msg):
            if ev.is_final_response() and ev.content and ev.content.parts:
                final_text = "".join(p.text or "" for p in ev.content.parts)

        dec = parse_decision_output(final_text)
        assert dec.chosen_action is None
        assert "violates the minimum floor of 10" in dec.reasoning

        mock_handlers = MagicMock(spec=ToolHandlers)
        res = execute_decision(dec, mock_handlers)
        assert res["status"] == "no_action"
        mock_handlers.set_container_cpu_weight.assert_not_called()

    asyncio.run(_test())

    # 2. Authoritative enforcement: even if a bypass proposes weight=5, the Safety Gate blocks it
    cfg = SafetyConfig.load()
    ctx = StaticContext(workloads={"rt-indexer": "UNKNOWN"}, host="BUILD")
    gate = SafetyGate(cfg, ctx)
    target = Target("container:rt-indexer", "c1", "rt-indexer", "img", "running", 100, 100, None, None)
    mock_sources = MagicMock()
    mock_sources.list.return_value = [target]
    mock_actuator = MagicMock()
    handlers = ToolHandlers(actuator=mock_actuator, targets=mock_sources, gate=gate)

    bad_dec = DecisionOutput(
        chosen_action=ChosenAction(tool="set_container_cpu_weight", target_id="container:rt-indexer", weight=5),
        reasoning="Attempting out-of-bounds weight",
    )
    res_blocked = execute_decision(bad_dec, handlers)
    assert res_blocked["status"] == "rejected"
    assert any("bounds: cpu.weight 5 outside policy bounds" in r for r in res_blocked["reasons"])
    mock_actuator.apply.assert_not_called()


def test_decision_agent_respects_ceiling():
    """Candidate above ceiling (weight > 1000) results in no_action; gate blocks if attempted."""
    dec = DecisionOutput(
        chosen_action=None,
        reasoning="Candidate proposed weight 1200 exceeding ceiling of 1000.",
    )
    mock_handlers = MagicMock(spec=ToolHandlers)
    res = execute_decision(dec, mock_handlers)
    assert res["status"] == "no_action"

    # Authoritative gate check
    cfg = SafetyConfig.load()
    ctx = StaticContext(workloads={"rt-indexer": "UNKNOWN"}, host="BUILD")
    gate = SafetyGate(cfg, ctx)
    target = Target("container:rt-indexer", "c1", "rt-indexer", "img", "running", 500, 100, None, None)
    mock_sources = MagicMock()
    mock_sources.list.return_value = [target]
    mock_actuator = MagicMock()
    handlers = ToolHandlers(actuator=mock_actuator, targets=mock_sources, gate=gate)

    bad_dec = DecisionOutput(
        chosen_action=ChosenAction(tool="set_container_cpu_weight", target_id="container:rt-indexer", weight=1200),
        reasoning="Attempting ceiling breach",
    )
    res_blocked = execute_decision(bad_dec, handlers)
    assert res_blocked["status"] == "rejected"
    assert any("bounds: cpu.weight 1200 outside policy bounds" in r for r in res_blocked["reasons"])
    mock_actuator.apply.assert_not_called()


def test_decision_agent_respects_max_jump():
    """Candidate exceeding 5x jump factor results in no_action; gate blocks if attempted."""
    # Current weight is 100 -> weight 10 is a 10x reduction (limit is 5x)
    cfg = SafetyConfig.load()
    ctx = StaticContext(workloads={"rt-indexer": "UNKNOWN"}, host="BUILD")
    gate = SafetyGate(cfg, ctx)
    target = Target("container:rt-indexer", "c1", "rt-indexer", "img", "running", 100, 100, None, None)
    mock_sources = MagicMock()
    mock_sources.list.return_value = [target]
    mock_actuator = MagicMock()
    handlers = ToolHandlers(actuator=mock_actuator, targets=mock_sources, gate=gate)

    bad_dec = DecisionOutput(
        chosen_action=ChosenAction(tool="set_container_cpu_weight", target_id="container:rt-indexer", weight=10),
        reasoning="10x jump",
    )
    res_blocked = execute_decision(bad_dec, handlers)
    assert res_blocked["status"] == "rejected"
    assert any("jump_size: cpu.weight 100 -> 10 is a 10.0x change" in r for r in res_blocked["reasons"])
    mock_actuator.apply.assert_not_called()


def test_decision_agent_no_valid_action_candidate():
    """When all candidate actions violate safety bounds, agent outputs no_action and execution is cleanly skipped."""
    dec = DecisionOutput(
        chosen_action=None,
        reasoning="All candidate actions violate safety bounds (jump > 5x or weight < 10).",
    )
    mock_handlers = MagicMock(spec=ToolHandlers)
    res = execute_decision(dec, mock_handlers)
    assert res["status"] == "no_action"
    assert "violate safety bounds" in res["reason"]


def test_adk_runner_agent_warning_and_isolation_regression(caplog):
    """Investigate and reproduce ADK Runner warning when session is shared across swapped runner.agent.
    
    Demonstrates:
    1. Reusing a session when swapping runner.agent (supervisor -> decision) causes ADK's
       _agent_router to log 'Event from an unknown agent: supervisor' because the session history
       contains events authored by an agent not in the new root agent's sub_agent tree.
    2. _agent_router falls back to the new root_agent (decision).
    3. The events yielded for the new turn are correctly attributed to 'decision' (no provenance corruption).
    4. Using a clean per-agent session avoids the warning completely and prevents prompt pollution.
    """
    import asyncio
    import logging
    from google.adk.agents import LlmAgent
    from google.adk.models import BaseLlm, LlmResponse
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    class MockLlm(BaseLlm):
        model: str = "mock"
        responses: list = []

        @classmethod
        def supported_models(cls):
            return ["mock"]

        async def generate_content_async(self, llm_request, stream=False):
            yield self.responses.pop(0)

    async def _test():
        sup_llm = MockLlm(responses=[
            LlmResponse(content=types.Content(role="model", parts=[types.Part(text="Supervisor done")]))
        ])
        dec_llm = MockLlm(responses=[
            LlmResponse(content=types.Content(role="model", parts=[types.Part(text='{"chosen_action": null, "reasoning": "None"}')]))
        ])

        sup_agent = LlmAgent(name="supervisor", model=sup_llm, instruction="sup", tools=[])
        dec_agent = LlmAgent(name="decision", model=dec_llm, instruction="dec", tools=[])

        runner = InMemoryRunner(agent=sup_agent, app_name="rt-test")
        shared_session = await runner.session_service.create_session(app_name="rt-test", user_id="u")

        # Step 1: Run Supervisor on shared session
        sup_msg = types.Content(role="user", parts=[types.Part(text="start supervisor")])
        sup_events = []
        async for ev in runner.run_async(user_id="u", session_id=shared_session.id, new_message=sup_msg):
            sup_events.append(ev)

        assert any(e.author == "supervisor" for e in sup_events)

        # Step 2: Swap runner.agent to decision_agent on SAME session
        runner.agent = dec_agent
        dec_msg = types.Content(role="user", parts=[types.Part(text="start decision")])

        with caplog.at_level(logging.WARNING):
            dec_events = []
            async for ev in runner.run_async(user_id="u", session_id=shared_session.id, new_message=dec_msg):
                dec_events.append(ev)

        # Verification 1: Warning reproduced
        warning_records = [
            r for r in caplog.records
            if "Event from an unknown agent: supervisor" in r.message
        ]
        assert len(warning_records) > 0, "Expected ADK _agent_router to log unknown agent warning for supervisor"

        # Verification 2: Event attribution and provenance
        # The events yielded for this invocation are strictly authored by 'decision'
        decision_replies = [e for e in dec_events if e.is_final_response()]
        assert len(decision_replies) == 1
        assert decision_replies[0].author == "decision"
        assert '{"chosen_action": null' in decision_replies[0].content.parts[0].text

        # Verification 3: Isolated session has NO warning
        dec_llm.responses.append(
            LlmResponse(content=types.Content(role="model", parts=[types.Part(text='{"chosen_action": null, "reasoning": "Clean"}')]))
        )
        clean_session = await runner.session_service.create_session(app_name="rt-test", user_id="u")
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            async for _ in runner.run_async(user_id="u", session_id=clean_session.id, new_message=dec_msg):
                pass
        unknown_in_clean = [
            r for r in caplog.records
            if "Event from an unknown agent" in r.message
        ]
        assert len(unknown_in_clean) == 0, "Clean session must not emit unknown agent warnings"

    asyncio.run(_test())

