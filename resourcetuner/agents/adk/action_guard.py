"""Degraded mode (b), enforced in code (decision 2026-10-24).

If any ADK agent's model call was answered by the local fallback model (Ollama), the plugin
ModelFallbackPlugin sets session state `llm_degraded = True`. From then on `DegradedActionBlocker`
refuses every ACTION tool call of that cycle before it runs — the tool body, and therefore the Safety
Gate, the MCP handlers and the Resource Controller, are never reached. It sits on ADK's
`before_tool_callback`, so it holds for any agent and any tool source (in-process function, MCP toolset)
and does not depend on what a prompt tells a model.

Crew findings have their own, per-finding rule (diagnostic_crew.crew.Finding.actionable): a diagnosis
written by the local model is logged but carries no action. `actions_for_decision` applies both rules.
"""

from __future__ import annotations

import logging

from google.adk.plugins import BasePlugin

from resourcetuner.mcp_server import tools as T

log = logging.getLogger("resourcetuner.guard")

# Every tool that can change the system, including ones switched off today.
ACTION_TOOLS = frozenset(set(T.TOOL_NAMES) - {"list_targets"})


class DegradedActionBlocker(BasePlugin):
    def __init__(self, name: str = "resourcetuner_degraded_action_blocker"):
        super().__init__(name=name)

    async def before_tool_callback(self, *, tool, tool_args, tool_context):
        if tool.name in ACTION_TOOLS and tool_context.state.get("llm_degraded"):
            record = {"tool": tool.name, "args": dict(tool_args or {}), "reason": "llm_degraded"}
            blocked = list(tool_context.state.get("blocked_actions") or [])
            tool_context.state["blocked_actions"] = blocked + [record]
            log.warning("BLOCKED %s %s: this cycle ran on the local fallback model (degraded mode)",
                        tool.name, tool_args)
            return {"status": "blocked", "error": "degraded_mode",
                    "message": "No action: this cycle used the local fallback model (degraded mode)."}
        return None


def actions_for_decision(findings: list[dict], llm_degraded: bool) -> list[dict]:
    """Candidate actions the Decision stage may even see: none in a degraded cycle, and only from
    findings that passed their deterministic checks and were written by a non-degraded model."""
    if llm_degraded:
        return []
    out = []
    for f in findings:
        action = (f.get("diagnosis") or {}).get("candidate_action")
        if f.get("actionable") and action:
            out.append({"agent": f["agent"], **action})
    return out
