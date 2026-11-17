"""Agent Card definitions for ResourceTuner according to the official A2A Protocol.

Implements the Canonical Data Model specified in Slides 96, 98-100, 103-104 of Dr. Shaikh's lecture:
- AgentCard: Digital business card describing identity, interfaces, capabilities, and skills.
- AgentSkill: Specific tasks the agent is capable of performing.
- Supported Interfaces: Where the agent's A2A endpoint lives.
"""

from __future__ import annotations

from typing import Any, Dict

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
)
from google.protobuf.json_format import MessageToDict


def get_crewai_agent_card(base_url: str = "http://localhost:8000/a2a/crew") -> AgentCard:
    """Return the official A2A AgentCard for the CrewAI Diagnostic Crew.

    Exposes the specialist diagnostic skills (CPU, RAM, IO) to client agents (e.g. ADK Supervisor).
    """
    cpu_skill = AgentSkill(
        id="cpu_contention_diagnosis",
        name="CPU Contention Diagnostic Specialist",
        description="Analyzes CPU PSI, rogue background processes, and calculates safe cgroup cpu.weight adjustments.",
        tags=["cgroups", "cpu", "psi", "throttling"],
        examples=["Analyze CPU contention when build container is throttled"],
    )

    ram_skill = AgentSkill(
        id="ram_saturation_diagnosis",
        name="Memory Saturation Diagnostic Specialist",
        description="Analyzes memory PSI, available memory percentages, and container OOM-kill delta events.",
        tags=["memory", "psi", "oom_kills"],
        examples=["Diagnose memory pressure during compiler peak allocation"],
    )

    io_skill = AgentSkill(
        id="io_bandwidth_diagnosis",
        name="IO Bandwidth Diagnostic Specialist",
        description="Analyzes block-IO wait ticks and background write bandwidth for disk bottlenecks.",
        tags=["io", "psi", "disk_write"],
        examples=["Diagnose disk write saturation during indexing"],
    )

    composite_skill = AgentSkill(
        id="diagnose_resource_contention",
        name="Full Multi-Specialist Resource Diagnostic",
        description="Executes composite CrewAI specialist diagnosis across selected subsystems (cpu, ram, io).",
        tags=["crewai", "multi_specialist", "diagnostics"],
        examples=["Run diagnostic crew for chosen subsystems ['cpu', 'ram']"],
    )

    return AgentCard(
        name="ResourceTuner CrewAI Diagnostic Crew",
        description="Multi-agent diagnostic crew running on CrewAI to analyze Linux cgroup v2 contention.",
        version="1.0.0",
        provider=AgentProvider(
            organization="ResourceTuner Autonomous Systems",
            url="https://github.com/resourcetuner",
        ),
        documentation_url="https://a2a-protocol.org/latest/topics/what-is-a2a/#a2a-and-adk",
        supported_interfaces=[
            AgentInterface(
                url=base_url,
                protocol_binding="HTTP/REST",
                protocol_version="1.0",
            )
        ],
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=False,
        ),
        skills=[composite_skill, cpu_skill, ram_skill, io_skill],
    )


def get_crewai_agent_card_dict(base_url: str = "http://localhost:8000/a2a/crew") -> Dict[str, Any]:
    """Return the JSON-serializable dictionary of the CrewAI AgentCard for the .well-known endpoint."""
    card = get_crewai_agent_card(base_url)
    return MessageToDict(card, preserving_proto_field_name=False)
