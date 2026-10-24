"""CPU Diagnostic Agent (CrewAI) + deterministic checks on its answer.

The agent gets PRE-SUMMARISED evidence (in production: the Statistics Engine + telemetry;
in Step 8a: hand-written), decides whether CPU contention is hurting the active workload,
names the culprit target(s), and suggests at most one action from the MCP tool surface.
It never acts: proposals go to the Decision Agent and then through the Safety Gate.

`check_diagnosis` is plain code that rejects answers which reference unknown targets, step
outside the tool schema, or cite numbers that are not in the evidence (no invented facts).
"""

from __future__ import annotations

import json

from crewai import Agent, Task

from resourcetuner.agents.diagnostic_crew.checks import CheckResult, common_issues, is_protected, targets_by_id

from resourcetuner.agents.diagnostic_crew.schemas import CpuDiagnosis
from resourcetuner.mcp_server import tools as T

ROLE = "CPU Diagnostic Agent"
GOAL = ("Decide from the supplied evidence whether CPU contention is slowing the active developer workload, "
        "identify which targets compete with it, and suggest at most one minimal, reversible CPU action.")
# Kept short on purpose: Groq allows 8000 tokens/min (measured), and the output schema travels in
# response_format, not in the prompt (see build_cpu_task).
BACKSTORY = ("Linux performance engineer. Reason only from the given measurements (PSI = % of time tasks "
             "waited for CPU; cpu_percent 100 = one core). Never invent numbers or give confidence scores. "
             "BUILD, TESTING and DEV_SERVER are what the developer waits for: never deprioritise them.")

TASK_TEMPLATE = """Evidence: {evidence}
Rules: use only target_ids from evidence.targets; name each evidence metric as section.field from the evidence (host.cpu_psi, trigger.threshold), or for a target its full target_id, a dot, then the field, with its exact value; at most ONE
action from {tools}; prefer the smallest reversible change (lower a background target's cpu weight) over
limits or pause; if contention does not hurt the workload set contention=false and candidate_action=null;
if you use history, cite the entry and its success counts."""


def tool_bounds() -> dict:
    return {
        "set_container_cpu_weight": f"weight integer {T.CPU_WEIGHT_MIN}-{T.CPU_WEIGHT_MAX} (default 100)",
        "set_container_cpu_limit": f"cpus number {T.CPU_LIMIT_MIN}-{T.CPU_LIMIT_MAX}",
        "pause_container": "no arguments",
    }


def build_cpu_agent(llm) -> Agent:
    # max_retry_limit=0: retries/fallback are the LLM router's job; CrewAI's own retries re-ran the
    # whole chain 3x on failure and burnt free-tier quota (measured 2026-10-22).
    return Agent(role=ROLE, goal=GOAL, backstory=BACKSTORY, llm=llm, tools=[], allow_delegation=False,
                 verbose=False, max_iter=2, max_retry_limit=0)


def build_cpu_task(agent: Agent, evidence: dict) -> Task:
    # response_model (not only output_pydantic): with it CrewAI does NOT paste the pretty-printed output
    # schema into the prompt (agent/utils.py: build_task_prompt_with_schema) — measured 4305 chars
    # (~45% of the prompt) duplicating what response_format json_schema already sends.
    return Task(
        description=TASK_TEMPLATE.format(evidence=json.dumps(evidence, separators=(",", ":")),
                                         tools=json.dumps(tool_bounds(), separators=(",", ":"))),
        expected_output="A CpuDiagnosis JSON object.",
        agent=agent,
        output_pydantic=CpuDiagnosis,
        response_model=CpuDiagnosis,
    )


# --- deterministic checks ---------------------------------------------------------------------

def check_diagnosis(d: CpuDiagnosis, evidence: dict) -> CheckResult:
    """Shared rules (checks.common_issues) + CPU-action rules."""
    issues = common_issues(d.culprit_target_ids, d.evidence, evidence)
    by_id = targets_by_id(evidence)
    a = d.candidate_action
    if a is not None:
        if a.tool not in T.ENABLED_TOOLS:
            issues.append(f"tool {a.tool} is not enabled")
        if a.target_id not in by_id:
            issues.append(f"action target {a.target_id!r} is not in the evidence")
        if a.tool == "set_container_cpu_weight" and not (a.weight is not None and
                                                         T.CPU_WEIGHT_MIN <= a.weight <= T.CPU_WEIGHT_MAX):
            issues.append(f"weight {a.weight} outside [{T.CPU_WEIGHT_MIN}, {T.CPU_WEIGHT_MAX}]")
        if a.tool == "set_container_cpu_limit" and not (a.cpus is not None and
                                                        T.CPU_LIMIT_MIN <= a.cpus <= T.CPU_LIMIT_MAX):
            issues.append(f"cpus {a.cpus} outside [{T.CPU_LIMIT_MIN}, {T.CPU_LIMIT_MAX}]")
        if not d.contention:
            issues.append("an action was proposed although contention=false")
        cur = by_id.get(a.target_id, {})
        reduces = ((a.tool == "set_container_cpu_weight" and a.weight is not None and
                    a.weight < cur.get("cpu_weight", 100)) or
                   (a.tool == "set_container_cpu_limit" and a.cpus is not None and
                    (cur.get("cpu_limit_cores") is None or a.cpus < cur["cpu_limit_cores"])) or
                   a.tool == "pause_container")
        if a.target_id in by_id and is_protected(a.target_id, evidence) and reduces:
            issues.append(f"action would reduce resources of the protected workload {cur.get('workload')}")
        if (a.tool == "set_container_cpu_weight" and a.weight == cur.get("cpu_weight")) or \
                (a.tool == "set_container_cpu_limit" and a.cpus == cur.get("cpu_limit_cores")):
            issues.append("action does not change anything (new value equals the current one)")
    return CheckResult(ok=not issues, issues=issues)
