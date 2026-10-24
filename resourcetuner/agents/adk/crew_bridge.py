"""Step 8c: the one function the Supervisor's tool calls to run the real CrewAI diagnostic crew.

ADK sees a plain `(resources, evidence) -> list[dict]` function; CrewAI, the LLM router, the agent gap
(Groq's 8000 tokens/min) and the per-agent evidence projection all live behind it.
"""

from __future__ import annotations

import time

from resourcetuner.agents.diagnostic_crew.crew import run_diagnostic_crew
from resourcetuner.agents.diagnostic_crew.evidence import project_evidence


def make_crew_diagnose(router=None, gap_s: float | None = None, sleep=time.sleep):
    def crew_diagnose(resources: list[str], evidence: dict) -> list[dict]:
        findings = run_diagnostic_crew(evidence, router=router, agents=tuple(resources), gap_s=gap_s,
                                       sleep=sleep, project=project_evidence)
        return [f.to_dict() for f in findings]
    return crew_diagnose
