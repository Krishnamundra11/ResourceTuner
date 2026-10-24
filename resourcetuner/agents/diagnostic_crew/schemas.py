"""Structured outputs shared by the diagnostic agents (CPU now; RAM/IO in Step 10).

No confidence field exists on purpose (brief §8): an agent may only cite measurable evidence
it was given, and the deterministic checker in cpu_agent.py verifies those numbers.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# The agent may only suggest actions that exist on the MCP tool surface (tools.ENABLED_TOOLS).
ActionTool = Literal["set_container_cpu_weight", "set_container_cpu_limit", "pause_container"]


class EvidenceItem(BaseModel):
    metric: str = Field(description="Name of a metric that appears in the evidence, e.g. 'host.cpu_psi'")
    value: float = Field(description="Its value copied exactly from the evidence")
    statement: str = Field(max_length=240, description="What this number shows")


class CandidateAction(BaseModel):
    tool: ActionTool
    target_id: str = Field(description="A target_id from the evidence")
    weight: int | None = Field(description="cpu.weight for set_container_cpu_weight, else null")
    cpus: float | None = Field(description="core limit for set_container_cpu_limit, else null")
    rationale: str = Field(max_length=300)


class CpuDiagnosis(BaseModel):
    resource: Literal["cpu"]
    contention: bool = Field(description="Is CPU contention hurting the active workload?")
    summary: str = Field(max_length=400)
    culprit_target_ids: list[str] = Field(description="target_ids competing with the workload; empty if none")
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=6)
    candidate_action: CandidateAction | None = Field(description="At most one reversible action, or null")
    historical_basis: str | None = Field(description="How the supplied history supports the action, or null")


class RamDiagnosis(BaseModel):
    """RAM agent: diagnosis and evidence ONLY (decision 2026-10-24). There is deliberately no
    action field: this VM has no swap, so no memory action is safely reversible."""
    resource: Literal["memory"]
    contention: bool = Field(description="Is there memory pressure, host-wide or inside a container's own limit?")
    scope: Literal["host", "container", "none"] = Field(
        description="host = the machine is short of memory; container = a container is hitting its own "
                    "memory limit (OOM kills / limit hits) while the host is fine; none = no pressure")
    summary: str = Field(max_length=400)
    culprit_target_ids: list[str] = Field(description="targets under or causing memory pressure; empty if none")
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=6)


IoActionTool = Literal["pause_container"]


class IoAction(BaseModel):
    tool: IoActionTool                 # the only I/O action allowed (decision 2026-10-24)
    target_id: str = Field(description="The background disk-hog to pause")
    rationale: str = Field(max_length=300)


class IoDiagnosis(BaseModel):
    resource: Literal["io"]
    contention: bool = Field(description="Is disk I/O contention hurting the active workload?")
    summary: str = Field(max_length=400)
    culprit_target_ids: list[str] = Field(description="targets competing for the disk; empty if none")
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=6)
    candidate_action: IoAction | None = Field(description="Pause of a background disk-hog, or null")
    historical_basis: str | None = Field(description="How the supplied history supports the action, or null")
