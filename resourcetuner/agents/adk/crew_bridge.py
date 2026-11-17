"""Step 8c & A2A Interoperability: Bridge between ADK Supervisor and CrewAI Diagnostic Crew.

Implements the official Agent-to-Agent (A2A) Protocol lifecycle:
1. Agent Discovery: Validates CrewAI's AgentCard and diagnostic skills.
2. Task Lifecycle: Submits an A2A Task, transitions state through SUBMITTED -> WORKING -> COMPLETED.
3. Execution: Runs the CrewAI specialist crew across the thread boundary.
4. Returns findings to the ADK Supervisor with zero breaking changes to existing callers.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, List, Optional

from resourcetuner.a2a.protocol import A2AInteroperabilityBridge
from resourcetuner.agents.diagnostic_crew.crew import run_diagnostic_crew
from resourcetuner.agents.diagnostic_crew.evidence import project_evidence

log = logging.getLogger("resourcetuner.adk.crew_bridge")
_BRIDGE = A2AInteroperabilityBridge()


def make_crew_diagnose(router=None, gap_s: float | None = None, sleep=time.sleep):
    """Factory returning a diagnose function that executes CrewAI via the A2A protocol."""
    def internal_crew_executor(resources: list[str], evidence: dict) -> list[dict]:
        findings = run_diagnostic_crew(
            evidence,
            router=router,
            agents=tuple(resources),
            gap_s=gap_s,
            sleep=sleep,
            project=project_evidence,
        )
        return [f.to_dict() for f in findings]

    def crew_diagnose(resources: list[str], evidence: dict) -> list[dict]:
        # Execute the diagnostic through the formal A2A Protocol lifecycle
        _, findings = _BRIDGE.execute_a2a_diagnosis(
            resources=resources,
            evidence=evidence,
            diagnose_fn=internal_crew_executor,
            client_id="adk:supervisor",
        )
        return findings

    return crew_diagnose
