"""A2A (Agent2Agent) Protocol Package for ResourceTuner."""

from resourcetuner.a2a.agent_card import get_crewai_agent_card, get_crewai_agent_card_dict
from resourcetuner.a2a.api import a2a_router
from resourcetuner.a2a.protocol import A2AInteroperabilityBridge, get_a2a_history

__all__ = [
    "get_crewai_agent_card",
    "get_crewai_agent_card_dict",
    "A2AInteroperabilityBridge",
    "get_a2a_history",
    "a2a_router",
]
