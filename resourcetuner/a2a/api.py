"""FastAPI / Starlette Router exposing official A2A Protocol Endpoints.

Implements the Protocol Bindings (Layer 3) specified in Slides 90-94 of Dr. Shaikh's lecture:
- GET /.well-known/agent-card.json: Agent discovery endpoint.
- POST /message:send: SendMessage API for remote task invocation.
- GET /history: Audit history of cross-framework A2A tasks.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from resourcetuner.a2a.agent_card import get_crewai_agent_card_dict
from resourcetuner.a2a.protocol import A2AInteroperabilityBridge, get_a2a_history

a2a_router = APIRouter(tags=["a2a"])
_BRIDGE = A2AInteroperabilityBridge()


class SendMessagePayload(BaseModel):
    client_id: str = "external_client"
    resources: List[str] = ["cpu"]
    evidence: Dict[str, Any] = {}
    context_id: str = "remote_a2a_call"


@a2a_router.get("/crew/.well-known/agent-card.json")
def get_agent_card() -> Dict[str, Any]:
    """A2A Step 1: Agent Discovery.

    Returns the digital business card describing identity, skills, and endpoints.
    """
    return get_crewai_agent_card_dict()


@a2a_router.post("/crew/message:send")
def send_message(payload: SendMessagePayload) -> Dict[str, Any]:
    """A2A Step 2 & 5: SendMessage API.

    Accepts an invocation request and dispatches a task to the CrewAI diagnostic crew.
    """
    from resourcetuner.agents.adk.crew_bridge import make_crew_diagnose

    crew_diagnose_fn = make_crew_diagnose()

    try:
        task, findings = _BRIDGE.execute_a2a_diagnosis(
            resources=payload.resources,
            evidence=payload.evidence,
            diagnose_fn=crew_diagnose_fn,
            context_id=payload.context_id,
            client_id=payload.client_id,
        )
        return {
            "task_id": task.id,
            "status": "COMPLETED",
            "findings": findings,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"A2A Task execution failed: {e}")


@a2a_router.get("/history")
def get_history() -> List[Dict[str, Any]]:
    """Return audit history of recent A2A cross-framework exchanges."""
    return get_a2a_history()
