"""Decision Agent (Google ADK): selects at most ONE candidate action or rejects (brief §3, §6).

Consumes validated findings from the diagnostic crew (via actions_for_decision) and
historical performance statistics from the Statistics Engine, then produces a structured
DecisionOutput (Pydantic).

Action execution is performed by the deterministic `execute_decision` executor, calling
ToolHandlers at the MCP boundary where SafetyGate and ResourceController enforce policy.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field

from resourcetuner.agents.adk.action_guard import actions_for_decision
from resourcetuner.mcp_server.tools import ToolHandlers

log = logging.getLogger("resourcetuner.decision")

# The base instruction is combined with dynamically-derived bounds at agent build time.
_BASE_INSTRUCTION = """\
You are the Decision Agent in ResourceTuner, an agentic resource-optimisation system.
You receive:
- The active workload and trigger context.
- Candidate actions proposed by diagnostic specialists (pre-verified and non-degraded).
- Historical performance statistics for relevant actions from the Policy Store (success rate, average improvement, sample count, trend, last outcome).
- **Deterministic action bounds** enforced by the Safety Gate. Any action outside these bounds will be rejected.

Your task:
1. Choose AT MOST ONE tuning action to execute, or choose null (or "no_action") if no action is warranted or candidates are unpromising.
2. If candidate_actions is empty, chosen_action MUST be null.
3. If an action's historical record shows high degradation, recent failures, or poor success rate (< 50% with >= 3 samples), do not choose it.
4. Strongly prefer actions that are marked 'promoted' or have a proven high success rate (>= 70%) with positive improvement.
5. In your reasoning, provide 1-2 concise sentences citing concrete numbers from the candidate actions or history.
6. NEVER invent numbers or output confidence percentages.
7. **Deterministic Safety Bounds**: You MUST respect the action bounds below. If no candidate action can produce a valid value within the bounds (e.g. proposed value violates the floor, ceiling, or maximum jump factor), you MUST return `no_action` (set chosen_action to null). DO NOT propose an out-of-bounds action for the Safety Gate to reject.
8. Return ONLY a valid JSON object matching the schema:
   {"chosen_action": {"tool": "...", "target_id": "...", "weight": 50, "cpus": null} | null, "reasoning": "..."}
   (or {"chosen_action": "no_action", "reasoning": "..."})
"""


def _format_bounds_instruction(safety_cfg) -> str:
    """Derive a human-readable bounds block from the same SafetyConfig used by the gate.

    This is the single source of truth: if safety.yaml changes, the prompt changes with it.
    """
    b = getattr(safety_cfg, "bounds", {})
    lines = ["## Deterministic Safety Bounds (enforced by the Safety Gate — violations will be rejected)"]

    # cpu.weight floor and ceiling
    cpu_lo, cpu_hi = b.get("cpu_weight", [10, 1000])
    lines.append(f"- **cpu.weight**: minimum {cpu_lo} (floor), maximum {cpu_hi} (ceiling)")

    # io.weight (if configured)
    if "io_weight" in b:
        io_lo, io_hi = b["io_weight"]
        lines.append(f"- **io.weight**: minimum {io_lo}, maximum {io_hi}")

    # cpu.max (cpu limit)
    if "cpu_limit_min_cores" in b:
        lines.append(f"- **cpu.max** (CPU quota): minimum {b['cpu_limit_min_cores']} cores (never throttle below this floor)")

    # jump size — stated as a FORMULA relative to the target's own current value, never a fixed
    # example. A fixed "e.g. weight 100 -> ..." example here is exactly what caused repeated
    # rejected cycles once a weight had already drifted below 100 from an earlier applied action
    # (2026-10-25 vertical slice: cycles 4-5 both proposed moves relative to an assumed baseline
    # instead of the target's real current weight, and the Gate correctly rejected both).
    max_jump = getattr(safety_cfg, "max_jump_factor", 5.0)
    lines.append(
        f"- **Maximum change per action**: {max_jump:.0f}x jump limit. For ANY candidate, let "
        f"`current` be THAT TARGET's own current cpu.weight as reported in its evidence entry "
        f"RIGHT NOW (never a fixed default, never a value from a previous cycle). The proposed "
        f"value must satisfy current/{max_jump:.0f} <= value <= current*{max_jump:.0f} AND the "
        f"floor/ceiling above. If `current` is already close to the floor, a further cut by "
        f"{max_jump:.0f}x is almost certainly infeasible — choose a smaller cut, pause_container, "
        f"or no_action instead of a value the Gate will reject."
    )
    cut_factor = getattr(safety_cfg, "cpu_limit_max_cut_factor", None)
    if cut_factor:
        lines.append(
            f"- **CPU limit cut**: existing limit may shrink by at most {cut_factor:.0f}x per action"
        )

    lines.append(
        "- If all candidates violate these bounds or no valid candidate remains within bounds, "
        "return `no_action` (chosen_action: null) with clear reasoning."
    )
    return "\n".join(lines)


class ChosenAction(BaseModel):
    tool: str
    target_id: str
    weight: int | None = None
    cpus: float | None = None


class DecisionOutput(BaseModel):
    chosen_action: ChosenAction | None = None
    reasoning: str


def build_decision_agent(model, safety_cfg=None) -> LlmAgent:
    """Construct the ADK Decision Agent without tools (structured output only).

    When *safety_cfg* is provided (or loaded from config/safety.yaml by default),
    action-bound information is derived directly from the deterministic policy configuration,
    so the prompt and deterministic gate can never drift.

    No max_output_tokens is set on Gemini models per project invariant.
    """
    if safety_cfg is None:
        try:
            from resourcetuner.safety.gate import SafetyConfig
            safety_cfg = SafetyConfig.load()
        except Exception as exc:
            log.warning("Could not load SafetyConfig for decision prompt: %s", exc)
            safety_cfg = None

    if safety_cfg is not None:
        instruction = _BASE_INSTRUCTION + "\n" + _format_bounds_instruction(safety_cfg)
    else:
        # Fallback if config cannot be loaded
        instruction = _BASE_INSTRUCTION + (
            "\n## Deterministic Safety Bounds\n"
            "- **cpu.weight**: minimum 10 (floor), maximum 1000 (ceiling)\n"
            "- **Maximum change per action**: 5x jump limit\n"
            "- If no valid action exists within these bounds, return `no_action` (chosen_action: null)."
        )
    return LlmAgent(name="decision", model=model, instruction=instruction, tools=[])


def decision_summary(evidence: dict, candidates: list[dict], stats_summary: dict) -> dict:
    """Format clean, bounded payload for the Decision Agent LLM."""
    wl = evidence.get("workload") or {}
    return {
        "workload": wl.get("class", "UNKNOWN"),
        "trigger": evidence.get("trigger"),
        "candidate_actions": candidates,
        "history_statistics": stats_summary,
    }


def parse_decision_output(text: str) -> DecisionOutput:
    """Robustly parse DecisionOutput from LLM response text."""
    clean = text.strip()
    # Strip markdown fences if present
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean, re.DOTALL)
    if match:
        clean = match.group(1).strip()
    else:
        # Look for the outermost JSON object
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            clean = clean[start : end + 1]

    try:
        data = json.loads(clean)
        # Handle "no_action" or null string representations
        ca = data.get("chosen_action")
        if ca in ("no_action", "null", "none", "None", ""):
            data["chosen_action"] = None
        elif isinstance(ca, dict) and ca.get("tool") in ("no_action", "null", "none", None):
            data["chosen_action"] = None
        return DecisionOutput.model_validate(data)
    except Exception as exc:
        log.warning("Failed to parse DecisionOutput from text %r: %s", text[:200], exc)
        return DecisionOutput(
            chosen_action=None,
            reasoning=f"Failed to parse decision JSON from model output: {clean[:120]}",
        )


def execute_decision(
    decision: DecisionOutput | None,
    handlers: ToolHandlers,
    llm_degraded: bool = False,
) -> dict[str, Any]:
    """Execute the chosen action through ToolHandlers with deterministic safety checks."""
    if llm_degraded:
        log.warning("execute_decision BLOCKED: cycle ran under degraded LLM mode")
        return {"status": "blocked", "error": "llm_degraded", "message": "Action blocked: cycle is degraded"}

    if decision is None or decision.chosen_action is None:
        reason = decision.reasoning if decision else "No decision"
        return {"status": "no_action", "reason": reason}

    action = decision.chosen_action
    tool = action.tool
    target_id = action.target_id

    if tool == "set_container_cpu_weight":
        if action.weight is None:
            return {"status": "error", "message": "Missing weight for set_container_cpu_weight"}
        return handlers.set_container_cpu_weight(target_id, action.weight, source_degraded=llm_degraded)

    if tool == "set_container_cpu_limit":
        if action.cpus is None:
            return {"status": "error", "message": "Missing cpus for set_container_cpu_limit"}
        return handlers.set_container_cpu_limit(target_id, action.cpus, source_degraded=llm_degraded)

    if tool == "pause_container":
        return handlers.pause_container(target_id, source_degraded=llm_degraded)

    return {"status": "error", "message": f"Unsupported action tool: {tool}"}
