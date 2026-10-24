"""Meta-Agent (Google ADK): governs policy lifecycle and promotion (§6 Policy & Meta-Agent).

Evaluates whether an action should be promoted to a preferred default policy in the
Policy Store or demoted.

To conserve quota and adhere to deterministic safety (approved decision 5-A):
- Runs after every evaluation verdict.
- Deterministic checks bypass LLM calls when sample counts are insufficient (< 5) or
  success rates are clearly below threshold (< 50% after >= 10 samples).
- LLM is called strictly for policy reasoning in borderline or promotion-ready cases.
- In degraded LLM cycles, promotion is strictly rejected without an LLM call.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from google.adk.agents import LlmAgent
from pydantic import BaseModel

from resourcetuner.policy.store import PolicyStore

log = logging.getLogger("resourcetuner.meta")

INSTRUCTION = """You are the Meta-Agent in ResourceTuner, responsible for policy governance and promotion.
You evaluate whether a tuning action should be promoted to a preferred default policy or demoted.

Promotion criteria:
- Strict promotion floor: sample_count >= 10, success_rate >= 0.80, and avg_improvement > 0.
- No early promotions allowed below 10 samples.
- Demotion: if a policy exhibits recent degradation (last_outcome == 'DEGRADED') or degrading trend, do not promote (promote=false).

Rules:
1. Output reasoning in 1-2 sentences citing exact numbers: sample_count, success_rate, avg_improvement, and recent_trend.
2. NEVER invent numbers or output confidence percentages.
3. Return ONLY a valid JSON object matching the schema:
   {"promote": true | false, "reasoning": "..."}"""


class MetaDecision(BaseModel):
    promote: bool
    reasoning: str


def build_meta_agent(model) -> LlmAgent:
    """Construct the ADK Meta-Agent without tools (structured output only).
    
    No max_output_tokens is set per project invariant.
    """
    return LlmAgent(name="meta", model=model, instruction=INSTRUCTION, tools=[])


def parse_meta_decision(text: str) -> MetaDecision:
    """Robustly parse MetaDecision from LLM response text."""
    clean = text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean, re.DOTALL)
    if match:
        clean = match.group(1).strip()
    else:
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            clean = clean[start : end + 1]

    try:
        data = json.loads(clean)
        return MetaDecision.model_validate(data)
    except Exception as exc:
        log.warning("Failed to parse MetaDecision from text %r: %s", text[:200], exc)
        return MetaDecision(
            promote=False,
            reasoning=f"Failed to parse meta JSON from model output: {clean[:120]}",
        )


def evaluate_policy_candidate(
    summary: dict[str, Any],
    llm_degraded: bool = False,
) -> tuple[bool, str | None]:
    """Deterministic pre-evaluation.
    
    Returns (should_call_llm, deterministic_reason_if_skipped).
    """
    if llm_degraded:
        return False, "Degraded LLM mode: policy promotion is blocked deterministically."

    sample_count = summary.get("sample_count", 0)
    success_rate = summary.get("success_rate", 0.0)
    last_outcome = summary.get("last_outcome", "")
    avg_improvement = summary.get("avg_improvement")

    # If recent outcome is DEGRADED, invoke LLM to reason about demotion
    if last_outcome == "DEGRADED":
        return True, None

    # Strict promotion floor: minimum 10 samples
    if sample_count < 10:
        return False, f"Deterministic floor: sample_count={sample_count} < 10; minimum 10 samples required for policy promotion."

    # Strict success rate floor: minimum 80% (0.80)
    if success_rate < 0.80:
        return False, f"Deterministic floor: success_rate={success_rate:.2f} < 0.80; minimum 80% success required for policy promotion."

    # Positive improvement required
    if avg_improvement is None or avg_improvement <= 0:
        return False, f"Deterministic floor: avg_improvement={avg_improvement} <= 0; positive improvement required for policy promotion."

    # Eligible candidate: invoke LLM for policy reasoning
    return True, None

