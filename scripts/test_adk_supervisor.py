"""The ADK Supervisor, live. Step 8b ran it against a mock crew; Step 8c (`--crew real`) against the
real CrewAI diagnostic crew (CPU / RAM / I/O agents, LLM chain Groq -> OpenRouter free -> Ollama).

Modes (the Supervisor's own model):
  gemini    native ADK Gemini (config adk_models.supervisor) + ModelFallbackPlugin
  groq      Groq gpt-oss-120b as the agent's model through ADK's LiteLlm (tool-calling check)
  fallback  a non-existent Gemini model: 404 -> the plugin answers from Groq, tools included

Scenarios (full combined evidence from diagnostic_crew/sample_evidence.py):
  cpu_ram   CPU trigger during BUILD, RAM at 18% available, history links RAM pressure to CPU saturation
            for BUILD -> expect cpu AND ram
  io        I/O trigger, CPU/RAM normal -> expect io only

--crew mock   canned findings (8b)         --crew real   the real crew, specialists run `agent_gap_s` apart

Usage: ./venv/bin/python scripts/test_adk_supervisor.py [--mode gemini|groq|fallback]
                                                         [--scenario cpu_ram|io|all] [--crew mock|real]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")

import yaml  # noqa: E402
from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from resourcetuner.agents.adk.action_guard import DegradedActionBlocker, actions_for_decision  # noqa: E402
from resourcetuner.agents.adk.model_plugin import ModelFallbackPlugin, gemini_model_for  # noqa: E402
from resourcetuner.agents.adk.supervisor import build_supervisor, mock_diagnose, supervisor_summary  # noqa: E402
from resourcetuner.agents.diagnostic_crew import sample_evidence as SE  # noqa: E402
from resourcetuner.agents.llm_router import load_dotenv  # noqa: E402

SCENARIOS = {
    "cpu_ram": {"expect": {"cpu", "ram"}, "evidence": SE.COMBINED_CPU_RAM},
    "io": {"expect": {"io"}, "evidence": SE.COMBINED_IO},
}


def model_for(mode: str, cfg: dict, override: str | None):
    if mode == "gemini":
        return override or gemini_model_for(cfg, "supervisor")
    if mode == "fallback":
        return "gemini-does-not-exist-404"
    from google.adk.models.lite_llm import LiteLlm
    return LiteLlm(model="groq/openai/gpt-oss-120b", api_key=os.environ["GROQ_API_KEY"], reasoning_effort="low")


def make_diagnose(crew: str):
    if crew == "mock":
        return mock_diagnose
    from resourcetuner.agents.adk.crew_bridge import make_crew_diagnose
    return make_crew_diagnose()          # default router (config/llm.yaml) and agent_gap_s


async def run_one(mode: str, name: str, cfg: dict, crew: str, model_override: str | None) -> dict:
    sc = SCENARIOS[name]
    agent = build_supervisor(model_for(mode, cfg, model_override), make_diagnose(crew))
    plugins = [DegradedActionBlocker()]
    if mode == "groq":   # Groq is the agent's own model here: strip thought parts before each call
        from resourcetuner.agents.adk.model_plugin import strip_thought_parts
        agent.before_model_callback = strip_thought_parts
    else:
        plugins.insert(0, ModelFallbackPlugin(cfg))
    runner = InMemoryRunner(agent=agent, app_name="rt-8c", plugins=plugins)
    session = await runner.session_service.create_session(app_name="rt-8c", user_id="u",
                                                          state={"evidence": sc["evidence"]})
    msg = types.Content(role="user", parts=[types.Part(
        text="Trigger summary: " + json.dumps(supervisor_summary(sc["evidence"])))])
    calls, final, t0 = [], "", time.monotonic()
    async for ev in runner.run_async(user_id="u", session_id=session.id, new_message=msg):
        for fc in ev.get_function_calls() or []:
            calls.append({"name": fc.name, "args": dict(fc.args or {})})
        if ev.is_final_response() and ev.content and ev.content.parts:
            final = "".join(p.text or "" for p in ev.content.parts)
    state = (await runner.session_service.get_session(app_name="rt-8c", user_id="u", session_id=session.id)).state
    chosen = set((state.get("delegation") or {}).get("resources", []))
    findings = state.get("findings") or []
    degraded = bool(state.get("llm_degraded"))
    return {
        "mode": mode, "crew": crew, "scenario": name, "seconds": round(time.monotonic() - t0, 1),
        "tool_calls": calls, "chosen": sorted(chosen), "expected": sorted(sc["expect"]),
        "match": chosen == sc["expect"], "reason": (state.get("delegation") or {}).get("reason"),
        "supervisor_provenance": state.get("llm_provenance"), "supervisor_degraded": degraded,
        "findings": [{"agent": f["agent"], "answered_by": f"{(f.get('provenance') or {}).get('provider')}/"
                                                          f"{(f.get('provenance') or {}).get('model')}",
                      "degraded": (f.get("provenance") or {}).get("degraded"), "check_ok": f.get("check_ok"),
                      "check_issues": f.get("check_issues"), "actionable": f.get("actionable"),
                      "contention": (f.get("diagnosis") or {}).get("contention"),
                      "culprits": [c.split(":", 1)[-1] for c in (f.get("diagnosis") or {}).get("culprit_target_ids", [])],
                      "error": f.get("error")} for f in findings],
        "actions_offered_to_decision": [(a["agent"], a["tool"], a["target_id"].split(":", 1)[-1],
                                         a.get("weight") or a.get("cpus")) for a in actions_for_decision(findings, degraded)],
        "final": final[:300],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["gemini", "groq", "fallback"], default="gemini")
    ap.add_argument("--scenario", choices=["cpu_ram", "io", "all"], default="all")
    ap.add_argument("--crew", choices=["mock", "real"], default="mock")
    ap.add_argument("--model", help="override the Gemini model (gemini mode), e.g. gemini-3.6-flash")
    args = ap.parse_args(argv)
    load_dotenv()
    cfg = yaml.safe_load((REPO / "config" / "llm.yaml").read_text())
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    ok = True
    for n in names:
        try:
            r = asyncio.run(run_one(args.mode, n, cfg, args.crew, args.model))
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"mode": args.mode, "scenario": n, "error": f"{type(e).__name__}: {str(e)[:300]}"}, indent=1))
            ok = False
            continue
        print(json.dumps(r, indent=1))
        ok &= r["match"] and len(r["tool_calls"]) == 1
        if args.crew == "real":
            ok &= all(f["check_ok"] for f in r["findings"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
