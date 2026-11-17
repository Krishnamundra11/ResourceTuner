"""A2A (Agent2Agent) Protocol Interaction Engine for ResourceTuner.

Implements the official 5-step A2A Interaction Lifecycle (Slides 92-96, 100, 106 of Dr. Shaikh's lecture):
1. Agent Discovery: Client inspects AgentCard.
2. Send Message / Create Task: Client sends Message with DataPart to initiate task.
3. Task Lifecycle Tracking: State transitions through SUBMITTED -> WORKING -> COMPLETED / FAILED.
4. Remote Agent Execution: CrewAI processes the diagnostic query.
5. Return Result: Returns Task response with structured DataPart finding payload.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from a2a.types import (
    AgentCard,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
)
from google.protobuf.json_format import MessageToDict

from resourcetuner.a2a.agent_card import get_crewai_agent_card, get_crewai_agent_card_dict

log = logging.getLogger("resourcetuner.a2a")

_A2A_HISTORY_LOCK = threading.Lock()
_A2A_HISTORY: List[Dict[str, Any]] = []


@dataclass
class A2ATaskRecord:
    task_id: str
    context_id: str
    client_id: str
    remote_agent_id: str
    state: str
    resources: List[str]
    created_at: float
    completed_at: Optional[float] = None
    findings_count: int = 0
    duration_ms: Optional[float] = None


def get_a2a_history() -> List[Dict[str, Any]]:
    """Return an audit history copy of all completed A2A tasks."""
    with _A2A_HISTORY_LOCK:
        return list(_A2A_HISTORY)


def clear_a2a_history() -> None:
    """Clear recorded A2A history (primarily for unit tests)."""
    with _A2A_HISTORY_LOCK:
        _A2A_HISTORY.clear()


class A2AInteroperabilityBridge:
    """Manages protocol-compliant A2A exchanges between Google ADK and CrewAI."""

    def __init__(self, agent_card: Optional[AgentCard] = None):
        self.agent_card = agent_card or get_crewai_agent_card()

    def discover_agent(self) -> Dict[str, Any]:
        """Step 1: Agent Discovery. Returns the JSON Agent Card."""
        return MessageToDict(self.agent_card, preserving_proto_field_name=False)

    def execute_a2a_diagnosis(
        self,
        resources: List[str],
        evidence: Dict[str, Any],
        diagnose_fn: Callable[[List[str], Dict[str, Any]], List[Dict[str, Any]]],
        *,
        context_id: str = "adk_session",
        client_id: str = "adk:supervisor",
    ) -> Tuple[Task, List[Dict[str, Any]]]:
        """Execute a full A2A Task according to the 5-step lifecycle.

        Args:
            resources: Subsystems requested by client agent (['cpu', 'ram']).
            evidence: Telemetry snapshot passed in DataPart.
            diagnose_fn: Execution function running CrewAI.
            context_id: Shared conversation / session ID.
            client_id: Identifier of client agent (ADK Supervisor).

        Returns:
            Tuple of (completed A2A Task, list of finding dictionaries).
        """
        task_id = f"a2a_task_{uuid.uuid4().hex[:12]}"
        t0 = time.time()

        # Step 2: Initialize Task in SUBMITTED state
        task = Task(
            id=task_id,
            context_id=context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
        )
        log.info("[A2A] Step 2: Task %s SUBMITTED by %s to %s for %s",
                 task_id, client_id, self.agent_card.name, resources)

        # Step 3: Transition to WORKING
        task.status.state = TaskState.TASK_STATE_WORKING
        log.info("[A2A] Step 3: Task %s state set to WORKING", task_id)

        # Step 4: Remote Agent Performs the Task (CrewAI execution)
        findings: List[Dict[str, Any]] = []
        try:
            findings = diagnose_fn(resources, evidence)
            task.status.state = TaskState.TASK_STATE_COMPLETED
            log.info("[A2A] Step 4 & 5: Task %s COMPLETED with %d finding(s)", task_id, len(findings))
        except Exception as e:
            task.status.state = TaskState.TASK_STATE_FAILED
            log.error("[A2A] Task %s FAILED during execution: %s", task_id, e)
            raise

        duration_ms = (time.time() - t0) * 1000.0

        # Step 5: Record audit record
        record = {
            "task_id": task_id,
            "context_id": context_id,
            "client_id": client_id,
            "remote_agent": self.agent_card.name,
            "state": "COMPLETED" if task.status.state == TaskState.TASK_STATE_COMPLETED else "FAILED",
            "resources": resources,
            "findings_count": len(findings),
            "duration_ms": round(duration_ms, 2),
            "timestamp": t0,
        }
        with _A2A_HISTORY_LOCK:
            _A2A_HISTORY.append(record)

        return task, findings
