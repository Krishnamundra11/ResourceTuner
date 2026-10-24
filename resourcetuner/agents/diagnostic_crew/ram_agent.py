"""RAM Diagnostic Agent (CrewAI): diagnosis + evidence only, never an action (decision 2026-10-24).

On this VM (no swap) memory pressure shows up as OOM kills and limit hits inside a capped
container, not as host memory PSI — so the agent must tell host-wide pressure from a container
fighting its own limit, and `check_ram_diagnosis` verifies that claim against the thresholds.
"""

from __future__ import annotations

import json

from crewai import Agent, Task

from resourcetuner.agents.diagnostic_crew.checks import CheckResult, common_issues, targets_by_id
from resourcetuner.agents.diagnostic_crew.schemas import RamDiagnosis

ROLE = "RAM Diagnostic Agent"
GOAL = "Decide from the evidence whether there is memory pressure, where (host or a container's own limit), and who causes it."
BACKSTORY = ("Linux memory engineer. The host has no swap, so pressure appears as OOM kills and memory-limit hits "
             "inside capped containers rather than as host memory PSI. Reason only from the given numbers; never "
             "invent numbers or give confidence scores. You only diagnose: there is no memory action to take.")
TASK_TEMPLATE = """Evidence: {evidence}
Rules: use only target_ids from evidence.targets; name each evidence metric as section.field from the evidence (host.cpu_psi, trigger.threshold), or for a target its full target_id, a dot, then the field, with its exact value; scope=host only if
host memory crosses evidence.thresholds, scope=container if a container shows OOM kills or limit hits while the host
is fine, scope=none (contention=false, no culprits) otherwise; never name a BUILD, TESTING or DEV_SERVER target as culprit."""


def build_ram_agent(llm) -> Agent:
    return Agent(role=ROLE, goal=GOAL, backstory=BACKSTORY, llm=llm, tools=[], allow_delegation=False,
                 verbose=False, max_iter=2, max_retry_limit=0)


def build_ram_task(agent: Agent, evidence: dict) -> Task:
    return Task(description=TASK_TEMPLATE.format(evidence=json.dumps(evidence, separators=(",", ":"))),
                expected_output="A RamDiagnosis JSON object.", agent=agent,
                output_pydantic=RamDiagnosis, response_model=RamDiagnosis)


def check_ram_diagnosis(d: RamDiagnosis, evidence: dict) -> CheckResult:
    issues = common_issues(d.culprit_target_ids, d.evidence, evidence)
    host, th = evidence.get("host", {}), evidence.get("thresholds", {})
    host_pressure = (host.get("mem_avail_pct", 100) < th.get("mem_avail_min_pct", 10)
                     or host.get("mem_psi", 0) >= th.get("mem_psi", 10))
    by_id = targets_by_id(evidence)
    starved = {tid for tid, t in by_id.items() if t.get("oom_kills_delta", 0) > 0 or t.get("limit_hits_delta", 0) > 0}
    if d.scope == "host" and not host_pressure:
        issues.append("scope=host but host memory is within the thresholds")
    if d.scope == "container":
        if not starved:
            issues.append("scope=container but no target shows OOM kills or limit hits")
        for tid in d.culprit_target_ids:
            if tid in by_id and tid not in starved:
                issues.append(f"culprit {tid} shows no OOM kills or limit hits")
    if d.scope == "none" and (d.contention or d.culprit_target_ids):
        issues.append("scope=none must have contention=false and no culprits")
    if d.scope != "none" and not d.contention:
        issues.append(f"scope={d.scope} but contention=false")
    if (host_pressure or starved) and d.scope == "none":
        issues.append("evidence shows memory pressure but scope=none")
    return CheckResult(ok=not issues, issues=issues)
