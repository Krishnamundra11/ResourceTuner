"""The diagnostic crew (CrewAI). Step 8a builds the CPU agent first, then RAM and I/O (brief);
Step 10 puts all three into real use with the Supervisor choosing between them.

The ADK Supervisor will call `run_diagnostic_crew` as ONE tool/function step and get back
structured findings; it does not know CrewAI runs underneath (brief §4).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

# CrewAI sends anonymous usage telemetry by default; this project keeps telemetry local.
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from crewai import Crew, Process  # noqa: E402

from resourcetuner.agents.diagnostic_crew.cpu_agent import (  # noqa: E402
    build_cpu_agent, build_cpu_task, check_diagnosis,
)
from resourcetuner.agents.diagnostic_crew.io_agent import build_io_agent, build_io_task, check_io_diagnosis  # noqa: E402
from resourcetuner.agents.diagnostic_crew.ram_agent import (  # noqa: E402
    build_ram_agent, build_ram_task, check_ram_diagnosis,
)
from resourcetuner.agents.diagnostic_crew.schemas import CpuDiagnosis, IoDiagnosis, RamDiagnosis  # noqa: E402
from resourcetuner.agents.llm_router import LLMRouter, RouterLLM  # noqa: E402


@dataclass
class Finding:
    agent: str
    diagnosis: CpuDiagnosis | RamDiagnosis | IoDiagnosis | None
    check_ok: bool
    check_issues: list[str]
    provenance: dict                      # provider, model, degraded, every attempt
    error: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        return bool(self.provenance.get("degraded"))

    @property
    def actionable(self) -> bool:
        """Degraded mode (b), decided 2026-10-24: if every cloud layer failed and the local model
        answered, the diagnosis is kept (flagged) for logs and the dashboard, but its action is
        'no action'. An answer that fails the deterministic checks is never actionable either.
        Anything that does go forward still passes the Safety Gate."""
        return self.diagnosis is not None and self.check_ok and not self.degraded

    @property
    def action_for_decision(self):
        a = getattr(self.diagnosis, "candidate_action", None)
        return a if self.actionable else None

    def to_dict(self) -> dict:
        reason = (None if self.actionable else
                  "degraded (local fallback model): no action" if self.degraded else
                  "failed deterministic checks" if self.diagnosis is not None else "no diagnosis")
        return {"agent": self.agent, "diagnosis": self.diagnosis.model_dump() if self.diagnosis else None,
                "check_ok": self.check_ok, "check_issues": self.check_issues,
                "actionable": self.actionable, "not_actionable_reason": reason,
                "provenance": self.provenance, "error": self.error}


AGENT_BUILDERS = {
    "cpu": (build_cpu_agent, build_cpu_task, check_diagnosis, CpuDiagnosis),
    "ram": (build_ram_agent, build_ram_task, check_ram_diagnosis, RamDiagnosis),
    "io": (build_io_agent, build_io_task, check_io_diagnosis, IoDiagnosis),
}


def run_diagnostic_crew(evidence: dict, router: LLMRouter | None = None, agents: tuple[str, ...] = ("cpu",),
                        gap_s: float | None = None, sleep=time.sleep, project=None) -> list[Finding]:
    """Run the requested diagnostic agents one after another, `gap_s` apart (default from
    config/llm.yaml diagnostic_crew.agent_gap_s), so a full CPU+RAM+IO round stays inside Groq's
    8000 tokens/min instead of three ~1.5k-token calls landing at once.

    `project(agent, evidence)` (e.g. evidence.project_evidence) gives each agent its own view of a
    combined evidence dict; its deterministic checks run against that same view."""
    router = router or LLMRouter.from_config()
    if gap_s is None:
        gap_s = float(router.config.get("diagnostic_crew", {}).get("agent_gap_s", 0))
    unknown = [n for n in agents if n not in AGENT_BUILDERS]
    if unknown:
        raise ValueError(f"agent(s) {unknown} not built yet")
    findings = []
    for i, name in enumerate(agents):
        if i and gap_s > 0:
            sleep(gap_s)
        build_agent, build_task, check_fn, schema = AGENT_BUILDERS[name]
        ev = project(name, evidence) if project else evidence
        llm = RouterLLM(router, chain="diagnostic")
        agent = build_agent(llm)
        task = build_task(agent, ev)
        crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
        try:
            out = crew.kickoff()
            diag = out.pydantic if isinstance(out.pydantic, schema) else schema.model_validate_json(out.raw)
            check = check_fn(diag, ev)
            findings.append(Finding(name, diag, check.ok, check.issues,
                                    llm.last_result.provenance() if llm.last_result else {}))
        except Exception as e:  # every provider failed, or the answer never validated
            attempts = getattr(e, "attempts", [])
            findings.append(Finding(name, None, False, [], {"attempts": [f"{a.provider}/{a.model}: {a.outcome}"
                                                                        for a in attempts]},
                                    error=f"{type(e).__name__}: {str(e)[:300]}"))
    return findings
