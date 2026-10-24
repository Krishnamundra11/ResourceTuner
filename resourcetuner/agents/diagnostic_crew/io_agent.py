"""I/O Diagnostic Agent (CrewAI). Its only possible action is pausing a BACKGROUND disk-hog
(decision 2026-10-24): io.weight is disabled (no exact rollback, no effect on this VM's
mq-deadline scheduler), and pause stops a writer's disk traffic and is exactly reversible.
"""

from __future__ import annotations

import json

from crewai import Agent, Task

from resourcetuner.agents.diagnostic_crew.checks import CheckResult, common_issues, is_protected, targets_by_id
from resourcetuner.agents.diagnostic_crew.schemas import IoDiagnosis

ROLE = "I/O Diagnostic Agent"
GOAL = "Decide from the evidence whether disk I/O contention slows the active workload, who causes it, and whether to pause one background disk-hog."
BACKSTORY = ("Linux storage engineer. io_psi = % of time tasks waited for the disk; iowait is CPU time idle waiting on "
             "I/O. Reason only from the given numbers; never invent numbers or give confidence scores. The only action "
             "available is pausing a background container; never pause BUILD, TESTING, DEV_SERVER or the active workload.")
TASK_TEMPLATE = """Evidence: {evidence}
Rules: use only target_ids from evidence.targets; name each evidence metric as section.field from the evidence (host.cpu_psi, trigger.threshold), or for a target its full target_id, a dot, then the field, with its exact value; at most ONE action:
pause_container on the background target that moves the most disk data; if I/O contention does not hurt the workload
set contention=false and candidate_action=null; if you use history, cite the entry and its success counts."""


def build_io_agent(llm) -> Agent:
    return Agent(role=ROLE, goal=GOAL, backstory=BACKSTORY, llm=llm, tools=[], allow_delegation=False,
                 verbose=False, max_iter=2, max_retry_limit=0)


def build_io_task(agent: Agent, evidence: dict) -> Task:
    return Task(description=TASK_TEMPLATE.format(evidence=json.dumps(evidence, separators=(",", ":"))),
                expected_output="An IoDiagnosis JSON object.", agent=agent,
                output_pydantic=IoDiagnosis, response_model=IoDiagnosis)


def _io_total(t: dict) -> float:
    return (t.get("io_read_mib_s") or 0) + (t.get("io_write_mib_s") or 0)


def check_io_diagnosis(d: IoDiagnosis, evidence: dict) -> CheckResult:
    issues = common_issues(d.culprit_target_ids, d.evidence, evidence)
    by_id = targets_by_id(evidence)
    a = d.candidate_action
    if a is not None:
        t = by_id.get(a.target_id)
        if t is None:
            issues.append(f"action target {a.target_id!r} is not in the evidence")
        else:
            if is_protected(a.target_id, evidence):
                issues.append(f"would pause the protected/active workload {t.get('workload')}")
            if t.get("state") == "paused":
                issues.append("target is already paused")
            background = [x for tid, x in by_id.items() if not is_protected(tid, evidence)]
            if background and _io_total(t) < max(_io_total(x) for x in background):
                issues.append("pause target is not the background target moving the most disk data")
        if not d.contention:
            issues.append("an action was proposed although contention=false")
    return CheckResult(ok=not issues, issues=issues)
