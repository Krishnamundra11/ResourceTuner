"""The Decision Agent's and the CPU agent's bounds instructions, as computable rules.

2026-10-25 vertical slice: two of five cycles were rejected because both prompts described the
jump-size rule with a fixed example ("weight 100 -> ..."), so once a weight had already drifted
down from an earlier applied action, the agent kept proposing moves relative to the wrong
baseline. These tests pin the fix: the instruction must state the rule as a formula relative to
the target's OWN current value, and must not contain a numeric example that could be mistaken
for an assumed starting point.
"""

from __future__ import annotations

from resourcetuner.agents.adk.decision import _format_bounds_instruction
from resourcetuner.agents.diagnostic_crew.cpu_agent import tool_bounds
from resourcetuner.mcp_server import tools as T
from resourcetuner.safety.gate import SafetyConfig


def test_decision_bounds_instruction_ties_the_jump_rule_to_the_targets_own_current_value():
    cfg = SafetyConfig.load()
    text = _format_bounds_instruction(cfg)

    assert f"{cfg.max_jump_factor:g}".split(".")[0] in text or f"{cfg.max_jump_factor:.0f}" in text
    assert "current" in text.lower()
    assert "own current" in text.lower() or "its own" in text.lower()
    # No fixed numeric example like "weight 100" that could be read as an assumed baseline.
    assert "weight 100" not in text
    assert "e.g. weight" not in text.lower()


def test_decision_bounds_instruction_reflects_the_configured_floor_and_ceiling():
    cfg = SafetyConfig.load()
    text = _format_bounds_instruction(cfg)
    lo, hi = cfg.bounds["cpu_weight"]
    assert str(lo) in text and str(hi) in text


def test_cpu_agent_tool_bounds_states_the_jump_rule_relative_to_the_targets_own_value():
    bounds = tool_bounds()
    weight_rule = bounds["set_container_cpu_weight"]
    assert "5x" in weight_rule
    assert "own current" in weight_rule.lower()
    assert "fixed default" in weight_rule.lower()   # explicitly warns against one, doesn't give one


def test_cpu_agent_tool_bounds_uses_the_real_policy_floor_not_the_raw_cgroup_range():
    # tools.CPU_WEIGHT_MIN/MAX (1-10000) is the raw kernel/cgroup range; config/safety.yaml's
    # policy floor/ceiling (10-1000 by default) is what the Gate actually enforces. The agent
    # must be told the policy numbers, or it will propose values the Gate rejects as a matter
    # of course (2026-10-25 vertical slice cycle 5: weight=5, "valid" under 1-10000, rejected
    # under the real floor of 10).
    cfg = SafetyConfig.load()
    lo, hi = cfg.bounds["cpu_weight"]
    bounds = tool_bounds(cfg)
    assert f"{lo}-{hi}" in bounds["set_container_cpu_weight"]
    assert str(T.CPU_WEIGHT_MAX) not in bounds["set_container_cpu_weight"] or hi == T.CPU_WEIGHT_MAX

    limit_lo = cfg.bounds["cpu_limit_min_cores"]
    assert str(limit_lo) in bounds["set_container_cpu_limit"]


def test_cpu_agent_tool_bounds_steers_away_from_a_floor_violating_cut():
    bounds = tool_bounds()
    weight_rule = bounds["set_container_cpu_weight"]
    assert "floor" in weight_rule.lower()
    assert "pause_container" in weight_rule or "no_action" in weight_rule
