"""Step 8c, offline end to end: ADK Supervisor -> real CrewAI crew (fake LLM transport) -> findings,
the agent gap, per-agent evidence projection, and degraded mode (b) enforced in code.

Nothing here calls a network: the Supervisor's model is a scripted ADK BaseLlm, the crew's LLM router
uses a fake transport, and the "controller" is the real ResourceController over fake container ops.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.tools import ToolContext
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_diagnostic_crew as TD  # noqa: E402  (router(), GOOD, M1..., Clock, FakeTransport helpers)

from resourcetuner.agents import llm_router as R  # noqa: E402
from resourcetuner.agents.adk import model_plugin as MP  # noqa: E402
from resourcetuner.agents.adk.action_guard import ACTION_TOOLS, DegradedActionBlocker, actions_for_decision  # noqa: E402
from resourcetuner.agents.adk.crew_bridge import make_crew_diagnose  # noqa: E402
from resourcetuner.agents.adk.supervisor import build_supervisor, supervisor_summary  # noqa: E402
from resourcetuner.agents.diagnostic_crew import sample_evidence as SE  # noqa: E402
from resourcetuner.agents.diagnostic_crew.evidence import project_evidence  # noqa: E402
from resourcetuner.controller.resource_controller import ActionStore, ResourceController  # noqa: E402
from resourcetuner.mcp_server.tools import ENABLED_TOOLS, Target, ToolHandlers  # noqa: E402
from resourcetuner.safety.gate import SafetyConfig, SafetyGate, StaticContext  # noqa: E402

IDX1 = "container:rt-build_vs_indexer-indexer-1"
IDX1_NAME = "rt-build_vs_indexer-indexer-1"


# --- scripted ADK model + response helpers ---------------------------------------------------------

class ScriptedLlm(BaseLlm):
    model: str = "scripted"
    script: list = []

    @classmethod
    def supported_models(cls):
        return ["scripted"]

    async def generate_content_async(self, llm_request, stream=False):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        yield item


def call(name, args):
    return LlmResponse(content=types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(name=name, args=args))]))


def say(text):
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


async def run_agent(agent, state, plugins=(), text="go"):
    runner = InMemoryRunner(agent=agent, app_name="rt-8c", plugins=list(plugins))
    s = await runner.session_service.create_session(app_name="rt-8c", user_id="u", state=state)
    tool_results = []
    async for ev in runner.run_async(user_id="u", session_id=s.id,
                                     new_message=types.Content(role="user", parts=[types.Part(text=text)])):
        for fr in ev.get_function_responses() or []:
            tool_results.append((fr.name, fr.response))
    final = await runner.session_service.get_session(app_name="rt-8c", user_id="u", session_id=s.id)
    return final.state, tool_results


CPU_DIAG = json.loads(json.dumps(TD.GOOD))                                   # indexer-1 weight 100 -> 25
RAM_DIAG = {"resource": "memory", "contention": False, "scope": "none", "culprit_target_ids": [],
            "summary": "Host memory is above the minimum and PSI is below its threshold; no pressure.",
            "evidence": [{"metric": "host.mem_avail_pct", "value": 18.0, "statement": "18% of memory available"},
                         {"metric": "host.mem_psi", "value": 6.2, "statement": "PSI 6.2, below the 10.0 threshold"}]}


def supervisor_with(crew_router, slept, resources=("cpu", "ram")):
    llm = ScriptedLlm(script=[call("run_diagnostics", {"resources": list(resources),
                                                       "reason": "CPU PSI 41.7 and history 7/10"}),
                              say("CPU and RAM were consulted.")])
    diagnose = make_crew_diagnose(router=crew_router, gap_s=20, sleep=slept.append)
    return build_supervisor(llm, diagnose)


# --- A. cloud path: Supervisor picks cpu+ram, the REAL crew runs them with a gap ------------------------

def test_supervisor_runs_the_real_crew_for_the_chosen_specialists(tmp_path):
    r, transport = TD.router(tmp_path, {TD.M1: [json.dumps(CPU_DIAG), json.dumps(RAM_DIAG)]})
    slept = []
    state, results = asyncio.run(run_agent(supervisor_with(r, slept), {"evidence": SE.COMBINED_CPU_RAM}))

    assert state["delegation"]["resources"] == ["cpu", "ram"]
    assert slept == [20]                                          # one gap, between the two agents
    assert [c["model"] for c in transport.calls] == [TD.M1, TD.M1]
    cpu_prompt = json.dumps(transport.calls[0]["messages"])
    ram_prompt = json.dumps(transport.calls[1]["messages"])
    assert "cpu_weight" in cpu_prompt and "mem_avail_pct" not in cpu_prompt    # per-agent evidence views
    assert "mem_avail_pct" in ram_prompt and "cpu_weight" not in ram_prompt
    f = {x["agent"]: x for x in state["findings"]}
    assert all(f[a]["check_ok"] and f[a]["actionable"] for a in ("cpu", "ram"))
    assert f["cpu"]["provenance"]["provider"] == "openrouter" and not f["cpu"]["provenance"]["degraded"]
    assert state["crew_degraded_agents"] == []
    offered = actions_for_decision(state["findings"], llm_degraded=False)
    assert [(a["agent"], a["tool"], a["target_id"], a["weight"]) for a in offered] == [
        ("cpu", "set_container_cpu_weight", IDX1, 25)]
    name, resp = results[0]
    assert name == "run_diagnostics" and [x["agent"] for x in resp["findings"]] == ["cpu", "ram"]
    assert resp["findings"][0]["action"]["target_id"] == IDX1                  # compact view reaches the LLM


def test_only_the_chosen_specialists_run(tmp_path):
    io_diag = json.loads(json.dumps(TD.IO_GOOD))
    r, transport = TD.router(tmp_path, {TD.M1: [json.dumps(io_diag)]})
    state, _ = asyncio.run(run_agent(supervisor_with(r, [], resources=("io",)), {"evidence": SE.COMBINED_IO}))
    assert state["delegation"]["resources"] == ["io"] and len(transport.calls) == 1
    assert [x["agent"] for x in state["findings"]] == ["io"]
    assert state["findings"][0]["check_ok"], state["findings"][0]["check_issues"]


def test_a_failed_check_makes_the_finding_non_actionable(tmp_path):
    bad = json.loads(json.dumps(CPU_DIAG))
    bad["culprit_target_ids"] = ["container:rt-build_vs_indexer-build"]       # the BUILD itself
    r, _ = TD.router(tmp_path, {TD.M1: [json.dumps(bad), json.dumps(RAM_DIAG)]})
    state, _ = asyncio.run(run_agent(supervisor_with(r, []), {"evidence": SE.COMBINED_CPU_RAM}))
    cpu = next(x for x in state["findings"] if x["agent"] == "cpu")
    assert not cpu["check_ok"] and not cpu["actionable"]
    assert actions_for_decision(state["findings"], False) == []


# --- B. crew degraded path: only the local model answered -> diagnosis kept, action dropped ---------------

def test_local_model_findings_are_logged_but_carry_no_action(tmp_path):
    script = {TD.M1: [R.ProviderError("503")], TD.GROQ: [R.NetworkError("down")],
              TD.OLLAMA: [json.dumps(CPU_DIAG), json.dumps(RAM_DIAG)]}
    r, _ = TD.router(tmp_path, script)
    state, _ = asyncio.run(run_agent(supervisor_with(r, []), {"evidence": SE.COMBINED_CPU_RAM}))
    cpu = next(x for x in state["findings"] if x["agent"] == "cpu")
    assert cpu["provenance"]["provider"] == "ollama" and cpu["provenance"]["degraded"] is True
    assert cpu["check_ok"] and cpu["diagnosis"]["candidate_action"] is not None      # diagnosis is kept...
    assert cpu["actionable"] is False and "degraded" in cpu["not_actionable_reason"]  # ...the action is not
    assert state["crew_degraded_agents"] == ["cpu", "ram"]
    assert actions_for_decision(state["findings"], llm_degraded=False) == []


# --- C. ADK-side degraded mode: nothing reaches the controller ----------------------------------------------

class FakeOps:
    def __init__(self):
        self.knobs, self.calls = {"cpu.weight": 100}, []

    def set_knob(self, cid, knob, value):
        self.calls.append((knob, value))
        self.knobs[knob] = value

    def read_knob(self, cid, knob):
        return self.knobs[knob]


class OneTarget:
    def list(self):
        return [Target("container:" + IDX1_NAME, "c" * 64, IDX1_NAME, "img", "running", 100, 100, None, None)]


def action_agent(model, tmp_path):
    """A stand-in Decision stage: an ADK agent whose only tool runs the REAL chain
    MCP handlers -> Safety Gate -> Resource Controller (over fake container ops)."""
    ops = FakeOps()
    store = ActionStore(tmp_path / "actions.db")
    gate = SafetyGate(SafetyConfig.load(), StaticContext(workloads={IDX1_NAME: "CODE_INDEXING"}, host="BUILD"))
    handlers = ToolHandlers(OneTarget(), ResourceController(store, ops), gate=gate)

    async def set_container_cpu_weight(target_id: str, weight: int, tool_context: ToolContext) -> dict:
        """Set a container's cpu weight."""
        return handlers.set_container_cpu_weight(target_id, weight)

    return LlmAgent(name="decision_stub", model=model, instruction="Apply the action.",
                    tools=[set_container_cpu_weight]), ops, store


def fallback_plugin(tmp_path, groq, ollama):
    p = MP.ModelFallbackPlugin(TD.config(tmp_path) | {"chains": {"adk": [
        {"provider": "gemini", "model": "gemini-x"},
        {"provider": "groq", "model": "gpt-oss", "litellm": "groq/gpt-oss"},
        {"provider": "ollama", "model": "qwen", "litellm": "ollama_chat/qwen"}]}}, call_log=None)
    p._models.update({"groq": groq, "ollama": ollama})
    return p


class FakeFallback:
    """Answers from a queue (the last item repeats), so a model that is asked again after a tool result
    can say "done" instead of calling the same tool forever (ADK stops that only at 500 LLM calls)."""

    def __init__(self, *results):
        self.results = list(results)

    async def generate_content_async(self, req, stream=False):
        item = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(item, Exception):
            raise item
        yield item


WANT = call("set_container_cpu_weight", {"target_id": IDX1, "weight": 25})


def test_degraded_cycle_never_reaches_the_controller(tmp_path):
    agent, ops, store = action_agent(ScriptedLlm(script=[RuntimeError("503")] * 5), tmp_path)
    plugin = fallback_plugin(tmp_path, groq=FakeFallback(RuntimeError("groq down")),
                             ollama=FakeFallback(WANT, say("blocked, nothing applied")))
    state, results = asyncio.run(run_agent(agent, {}, plugins=[plugin, DegradedActionBlocker()]))

    assert state["llm_degraded"] is True                                     # the local model drove this cycle
    assert state["llm_provenance"]["decision_stub"]["provider"] == "ollama"
    assert results and results[0][1]["status"] == "blocked" and results[0][1]["error"] == "degraded_mode"
    assert ops.calls == []                                                    # the controller was never touched
    assert store.history() == []                                              # not even a pending record
    assert state["blocked_actions"][0]["tool"] == "set_container_cpu_weight"


def test_the_same_action_goes_through_gate_and_controller_when_not_degraded(tmp_path):
    agent, ops, store = action_agent(ScriptedLlm(script=[RuntimeError("503")] * 5), tmp_path)
    plugin = fallback_plugin(tmp_path, groq=FakeFallback(WANT, say("applied")),
                             ollama=FakeFallback(RuntimeError("unused")))
    state, results = asyncio.run(run_agent(agent, {}, plugins=[plugin, DegradedActionBlocker()]))

    assert "llm_degraded" not in state                                        # Groq answered: not degraded
    assert state["llm_provenance"]["decision_stub"]["provider"] == "groq"
    assert results[0][1]["status"] == "applied" and results[0][1]["gate"] == "approved"
    assert ops.calls == [("cpu.weight", 25)]
    assert [r["status"] for r in store.history()] == ["applied"]


def test_the_blocker_still_applies_without_the_fallback_plugin(tmp_path):
    """The rule is on the state flag, not on how the flag got set."""
    agent, ops, _ = action_agent(ScriptedLlm(script=[WANT, say("done")]), tmp_path)
    state, results = asyncio.run(run_agent(agent, {"llm_degraded": True}, plugins=[DegradedActionBlocker()]))
    assert results[0][1]["status"] == "blocked" and ops.calls == []


@pytest.mark.parametrize("tool", sorted(ACTION_TOOLS))
def test_every_action_tool_is_blocked_when_degraded(tool):
    class Tool:
        name = tool

    class Ctx:
        state = {"llm_degraded": True}

    out = asyncio.run(DegradedActionBlocker().before_tool_callback(tool=Tool(), tool_args={"target_id": "x"},
                                                                    tool_context=Ctx()))
    assert out["status"] == "blocked" and Ctx.state["blocked_actions"][0]["tool"] == tool


def test_read_only_and_non_degraded_calls_are_not_blocked():
    class Ctx:
        def __init__(self, degraded):
            self.state = {"llm_degraded": degraded}

    class ReadOnly:
        name = "list_targets"

    class Action:
        name = "pause_container"

    guard = DegradedActionBlocker()
    assert asyncio.run(guard.before_tool_callback(tool=ReadOnly(), tool_args={}, tool_context=Ctx(True))) is None
    assert asyncio.run(guard.before_tool_callback(tool=Action(), tool_args={}, tool_context=Ctx(False))) is None
    assert ACTION_TOOLS == set(ENABLED_TOOLS) - {"list_targets"} | {"set_container_io_weight"}


def test_actions_for_decision_is_empty_in_a_degraded_cycle():
    findings = [{"agent": "cpu", "actionable": True, "diagnosis": {"candidate_action": {"tool": "pause_container",
                                                                                         "target_id": IDX1}}}]
    assert len(actions_for_decision(findings, llm_degraded=False)) == 1
    assert actions_for_decision(findings, llm_degraded=True) == []


# --- D. config and pieces --------------------------------------------------------------------------------

def test_gemini_models_come_from_config_and_no_output_cap_is_set():
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "llm.yaml").read_text())
    assert {a: MP.gemini_model_for(cfg, a) for a in ("supervisor", "decision", "meta")} == {
        "supervisor": "gemini-3.5-flash-lite", "decision": "gemini-3.6-flash", "meta": "gemini-3.6-flash"}
    agent = build_supervisor("gemini-3.5-flash-lite", lambda r, e: [])
    cfg_obj = agent.generate_content_config
    assert cfg_obj is None or cfg_obj.max_output_tokens is None


def test_supervisor_summary_and_projection_are_deterministic():
    s = supervisor_summary(SE.COMBINED_CPU_RAM)
    assert s["cpu"]["top_background_cpu_percent"] == 65.2 and s["ram"]["mem_avail_pct"] == 18.0
    assert any(h.get("observation") == "ram_pressure_preceded_cpu_saturation" for h in s["history"])
    ram = project_evidence("ram", SE.COMBINED_CPU_RAM)
    assert "history" not in ram and "cpu_weight" not in json.dumps(ram)
    assert [h["resource"] for h in project_evidence("cpu", SE.COMBINED_CPU_RAM)["history"]] == ["cpu"]
