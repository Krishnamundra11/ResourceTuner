"""Supervisor Agent (Google ADK): decides WHICH diagnostic specialists to consult (brief §3).

Not "CPU high -> CPU agent only": it reads the trigger, all three resources' summaries and the
pre-summarised history (e.g. "RAM pressure often precedes CPU saturation for BUILD") and may
consult more than the triggering resource. It acts through ONE tool, `run_diagnostics`, which
calls the diagnostic crew as a single step — ADK does not know CrewAI runs underneath (brief §4).

The evidence the crew sees comes from session state, never from the LLM's arguments: the model
chooses *which* specialists and *why*, it cannot inject or alter numbers.

Step 8b ran this against `mock_diagnose`; Step 8c passes `crew_bridge.crew_diagnose` instead.
Gemini model names come from config/llm.yaml (`adk_models`); no max_output_tokens is set.
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable

from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext

from resourcetuner.agents.diagnostic_crew.checks import is_protected

RESOURCES = ("cpu", "ram", "io")

INSTRUCTION = """You are the Supervisor of a resource-optimisation system on a developer machine.
The user message holds a deterministic trigger summary: which threshold fired, the active workload,
per-resource summaries, and pre-computed history statistics.

Decide which diagnostic specialists to consult: "cpu", "ram", "io". Do not just pick the resource that
triggered — use the summaries and history (e.g. a correlation between RAM pressure and CPU saturation for
this workload) to include another specialist when the numbers support it, and leave out resources whose
numbers are normal. Call run_diagnostics exactly once with your choice and a one-sentence reason that
quotes the numbers you relied on. Then reply with a short summary of the findings it returned.
Never invent numbers and never state confidence percentages."""

DiagnoseFn = Callable[[list[str], dict], list[dict]]


def supervisor_summary(evidence: dict) -> dict:
    """What the Supervisor LLM sees: a few numbers per resource, computed by plain code."""
    host, th = evidence.get("host", {}), evidence.get("thresholds", {})
    background = [t for t in evidence.get("targets", []) if not is_protected(t["target_id"], evidence)]

    def top(field: str):
        vals = [t[field] for t in background if t.get(field) is not None]
        return max(vals) if vals else 0.0

    wl = evidence.get("workload", {})
    return {
        "trigger": evidence.get("trigger"),
        "workload": {"class": wl.get("class"), "confidence": wl.get("confidence")},
        "cpu": {"host_cpu_psi": host.get("cpu_psi"), "host_cpu_percent": host.get("cpu_percent"),
                "cpu_psi_threshold": th.get("cpu_psi"), "top_background_cpu_percent": top("cpu_percent")},
        "ram": {"mem_avail_pct": host.get("mem_avail_pct"), "mem_psi": host.get("mem_psi"),
                "mem_psi_threshold": th.get("mem_psi"), "mem_avail_min_pct": th.get("mem_avail_min_pct"),
                "container_oom_kills": sum(t.get("oom_kills_delta", 0) for t in evidence.get("targets", []))},
        "io": {"io_psi": host.get("io_psi"), "io_psi_threshold": th.get("io_psi"), "iowait": host.get("iowait"),
               "top_background_write_mib_s": top("io_write_mib_s")},
        "history": evidence.get("history", []),
    }


def compact_finding(f: dict) -> dict:
    """What goes back to the Supervisor LLM (the full finding stays in session state)."""
    d = f.get("diagnosis") or {}
    a = d.get("candidate_action")
    p = f.get("provenance") or {}
    return {
        "agent": f.get("agent"), "contention": d.get("contention"), "scope": d.get("scope"),
        "culprits": d.get("culprit_target_ids", []), "summary": (d.get("summary") or "")[:240],
        "action": ({k: a.get(k) for k in ("tool", "target_id", "weight", "cpus")} if a and f.get("actionable") else None),
        "check_ok": f.get("check_ok"), "actionable": f.get("actionable"),
        "not_actionable_reason": f.get("not_actionable_reason"), "degraded": bool(p.get("degraded")),
        "answered_by": f"{p.get('provider')}/{p.get('model')}" if p.get("provider") else None, "error": f.get("error"),
    }


def make_run_diagnostics(diagnose: DiagnoseFn):
    async def run_diagnostics(resources: list[str], reason: str, tool_context: ToolContext) -> dict:
        """Consult the diagnostic specialists.

        Args:
          resources: which specialists to run, a subset of ["cpu", "ram", "io"].
          reason: one sentence explaining the choice, quoting the numbers used.
        """
        chosen = [r for r in dict.fromkeys(resources) if r in RESOURCES]
        rejected = [r for r in resources if r not in RESOURCES]
        evidence = json.loads(json.dumps(tool_context.state.get("evidence") or {}))
        # The crew is blocking code (CrewAI + sleeps between agents): keep it off the event loop.
        findings = await asyncio.to_thread(diagnose, chosen, evidence) if chosen else []
        tool_context.state["delegation"] = {"resources": chosen, "rejected": rejected, "reason": reason}
        tool_context.state["findings"] = findings
        tool_context.state["crew_degraded_agents"] = [
            f["agent"] for f in findings if (f.get("provenance") or {}).get("degraded")]
        return {"consulted": chosen, "ignored_unknown": rejected, "findings": [compact_finding(f) for f in findings]}
    return run_diagnostics


def build_supervisor(model, diagnose: DiagnoseFn) -> LlmAgent:
    # No generate_content_config: max_output_tokens must stay unset on Gemini agents (user decision).
    return LlmAgent(name="supervisor", model=model, instruction=INSTRUCTION,
                    tools=[make_run_diagnostics(diagnose)])


def mock_diagnose(resources: list[str], evidence: dict) -> list[dict]:
    """Stand-in for the CrewAI crew (Step 8b): canned, clearly-labelled findings per resource."""
    canned = {
        "cpu": {"contention": True, "culprit_target_ids": ["container:mock-indexer"],
                "summary": "MOCK: indexer competes with the build for CPU",
                "candidate_action": {"tool": "set_container_cpu_weight", "target_id": "container:mock-indexer",
                                     "weight": 50}},
        "ram": {"contention": False, "scope": "none", "culprit_target_ids": [], "summary": "MOCK: no memory pressure"},
        "io": {"contention": False, "culprit_target_ids": [], "summary": "MOCK: disk quiet", "candidate_action": None},
    }
    return [{"agent": r, "mock": True, "actionable": True, "check_ok": True, "diagnosis": canned[r],
             "provenance": {"provider": "mock", "model": "mock"}} for r in resources]
