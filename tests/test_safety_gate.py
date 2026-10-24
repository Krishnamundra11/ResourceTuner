"""Safety Gate unit tests with hand-written fake proposed actions (brief Step 6), no LLM."""

import pytest

from resourcetuner.mcp_server.tools import (
    ENABLED_TOOLS, DryRunActuator, ProposedAction, Target, ToolHandlers,
)
from resourcetuner.safety.gate import SafetyConfig, SafetyGate, StaticContext, reduces_resources

NOW = 1_000_000.0
BUILD, IDX, HOG, TESTS = "rt-s-build", "rt-s-indexer", "rt-s-hog", "rt-s-tests"
KNOB = {"set_container_cpu_weight": "cpu.weight", "set_container_io_weight": "io.weight",
        "set_container_cpu_limit": "cpu.max", "pause_container": "freeze"}


def target(name, state="running"):
    return Target("container:" + name, "c" * 64, name, "img", state, 100, 100, None, None)


def action(name, tool, old, new, knob=None, target_id=None, reversible=True):
    return ProposedAction("a1", tool, target_id or "container:" + name, "c" * 64, name,
                          knob or KNOB[tool], old, new, reversible)


def gate(host="BUILD", workloads=None, last=None, in_flight=None, **cfg):
    ctx = StaticContext(
        workloads=workloads if workloads is not None else
        {BUILD: "BUILD", IDX: "CODE_INDEXING", HOG: "UNKNOWN", TESTS: "TESTING"},
        host=host, last_actions=last or {}, in_flight=in_flight or [])
    return SafetyGate(SafetyConfig(**cfg), ctx, clock=lambda: NOW)


def check(g, a, name=None):
    return g.evaluate(a, target(name or a.container_name))


def failed(v):
    return {r.split(":")[0] for r in v.reasons}


# --- APPROVE cases ---------------------------------------------------------------------

@pytest.mark.parametrize("a", [
    action(IDX, "set_container_cpu_weight", 100, 20),       # the Step 9 milestone action
    action(HOG, "set_container_cpu_limit", None, 1.0),      # throttle an unknown CPU hog
    action(HOG, "set_container_cpu_weight", 100, 25),       # deprioritise an unknown hog
    action(IDX, "pause_container", "running", "paused"),    # pause a background indexer
    action(BUILD, "set_container_cpu_weight", 100, 400),    # BOOST the protected build: allowed
    action(BUILD, "set_container_cpu_limit", 1.0, 2.0),     # relax a limit on the build: allowed
])
def test_safe_actions_are_approved(a):
    v = check(gate(), a)
    assert v.approved, v.reasons
    assert v.reasons == [] and all(c.endswith(": ok") for c in v.checks) and len(v.checks) == 10


def test_unknown_host_workload_does_not_protect_a_lone_hog():
    v = check(gate(host="UNKNOWN"), action(HOG, "set_container_cpu_limit", None, 1.0))
    assert v.approved, v.reasons


def test_cooldown_expired_is_approved():
    v = check(gate(last={"container:" + IDX: NOW - 50}), action(IDX, "set_container_cpu_weight", 100, 50))
    assert v.approved


# --- REJECT cases, one per rule -----------------------------------------------------------

@pytest.mark.parametrize("a,host,rule", [
    (action(BUILD, "set_container_cpu_weight", 100, 50), "BUILD", "foreground"),     # protected class
    (action(TESTS, "pause_container", "running", "paused"), "BUILD", "foreground"),  # pause TESTING
    (action(BUILD, "set_container_cpu_limit", None, 2.0), "TESTING", "foreground"),  # BUILD even if not host
    (action(IDX, "set_container_cpu_weight", 100, 50), "CODE_INDEXING", "foreground"),  # indexer IS the work
])
def test_protected_workloads_are_never_reduced(a, host, rule):
    v = check(gate(host=host), a)
    assert not v.approved and failed(v) == {rule}


def test_unknown_workload_fails_closed():
    v = check(gate(workloads={}), action(IDX, "set_container_cpu_weight", 100, 50))
    assert not v.approved and "fail closed" in v.reasons[0]


def test_jump_too_large():
    v = check(gate(), action(IDX, "set_container_cpu_weight", 100, 10))   # 10x > 5x
    assert failed(v) == {"jump_size"}


@pytest.mark.parametrize("a,expected", [
    (action(IDX, "set_container_cpu_weight", 100, 5), {"bounds", "jump_size"}),
    (action(BUILD, "set_container_cpu_weight", 100, 2000), {"bounds", "jump_size"}),
    (action(HOG, "set_container_cpu_limit", None, 0.3), {"bounds"}),
    (action(HOG, "set_container_cpu_limit", 4.0, 0.5), {"jump_size"}),     # 8x cut > 4x
])
def test_policy_bounds_and_cuts(a, expected):
    v = check(gate(), a)
    assert not v.approved and failed(v) == expected


def test_cooldown_active():
    v = check(gate(last={"container:" + IDX: NOW - 10}), action(IDX, "set_container_cpu_weight", 100, 50))
    assert failed(v) == {"cooldown"} and "10 s ago" in v.reasons[0]


def test_one_action_in_flight_blocks_the_next_anywhere():
    v = check(gate(in_flight=["container:" + HOG]), action(IDX, "set_container_cpu_weight", 100, 50))
    assert failed(v) == {"in_flight"}


def test_forged_knob_is_rejected():
    v = check(gate(), action(IDX, "set_container_cpu_weight", 100, 50, knob="memory.max"))
    assert "allowed_action" in failed(v)


def test_unknown_tool_is_rejected():
    forged = ProposedAction("a", "kill_container", "container:" + IDX, "c" * 64, IDX, "kill", "running", "dead")
    v = check(gate(), forged)
    assert "allowed_action" in failed(v) and not v.approved


def test_target_mismatch_is_rejected():
    v = check(gate(), action(IDX, "set_container_cpu_weight", 100, 50, target_id="container:" + BUILD), name=IDX)
    assert "allowed_target" in failed(v)


def test_protected_name_pattern():
    v = check(gate(protected_name_patterns=["rt-s-ind*"]), action(IDX, "set_container_cpu_weight", 100, 50))
    assert failed(v) == {"allowed_target"}


def test_exited_target_is_rejected():
    a = action(IDX, "set_container_cpu_weight", 100, 50)
    assert "allowed_target" in failed(gate().evaluate(a, target(IDX, state="exited")))


def test_unknown_old_value_is_not_reversible():
    v = check(gate(), action(IDX, "set_container_cpu_weight", None, 50))
    assert "reversible" in failed(v)


def test_irreversible_flag_is_rejected():
    v = check(gate(), action(IDX, "set_container_cpu_weight", 100, 50, reversible=False))
    assert "reversible" in failed(v)


def test_pause_disabled_by_policy():
    v = check(gate(allow_pause=False), action(IDX, "pause_container", "running", "paused"))
    assert failed(v) == {"pause"}


def test_all_failures_are_reported_together():
    v = check(gate(last={"container:" + BUILD: NOW - 5}, in_flight=["container:" + HOG]),
              action(BUILD, "set_container_cpu_weight", 100, 5))
    assert failed(v) == {"bounds", "jump_size", "foreground", "cooldown", "in_flight"}


def test_degraded_source_is_rejected():
    from resourcetuner.mcp_server.tools import ProposedAction
    a = ProposedAction("a1", "set_container_cpu_weight", "container:" + IDX, "c" * 64, IDX,
                       "cpu.weight", 100, 50, source_degraded=True)
    v = check(gate(), a)
    assert not v.approved and failed(v) == {"degraded_source"}
    assert "local fallback model" in v.reasons[0]


def test_non_degraded_source_is_not_rejected():
    from resourcetuner.mcp_server.tools import ProposedAction
    a = ProposedAction("a1", "set_container_cpu_weight", "container:" + IDX, "c" * 64, IDX,
                       "cpu.weight", 100, 50, source_degraded=False)
    v = check(gate(), a)
    assert v.approved, v.reasons


def test_crashing_context_rejects_instead_of_approving():
    class Broken(StaticContext):
        def workload_of(self, name):
            raise RuntimeError("classifier unavailable")

    g = SafetyGate(SafetyConfig(), Broken(), clock=lambda: NOW)
    v = g.evaluate(action(IDX, "set_container_cpu_weight", 100, 50), target(IDX))
    assert not v.approved and "check error" in v.reasons[0]


def test_reduces_resources():
    assert reduces_resources(action(IDX, "set_container_cpu_weight", 100, 20))
    assert not reduces_resources(action(IDX, "set_container_cpu_weight", 100, 200))
    assert reduces_resources(action(IDX, "set_container_cpu_limit", None, 3.0))
    assert not reduces_resources(action(IDX, "set_container_cpu_limit", 1.0, 3.0))
    assert reduces_resources(action(IDX, "pause_container", "running", "paused"))


# --- wired into the MCP tool layer -------------------------------------------------------

class Targets:
    def list(self):
        return [target(BUILD), target(IDX)]


def test_gate_inside_tool_layer_blocks_before_actuator():
    act = DryRunActuator()
    h = ToolHandlers(Targets(), act, gate=gate())
    rejected = h.set_container_cpu_weight("container:" + BUILD, 50)    # deprioritise the build
    approved = h.set_container_cpu_weight("container:" + IDX, 20)      # deprioritise the indexer
    assert rejected["status"] == "rejected" and "foreground" in rejected["reasons"][0]
    assert approved["status"] == "dry_run" and approved["gate"] == "approved"
    assert [a.container_name for a in act.actions] == [IDX]


def test_config_file_matches_tool_surface():
    cfg = SafetyConfig.load()
    assert set(cfg.allowed_actions) == set(ENABLED_TOOLS) - {"list_targets"}
    assert 30 <= cfg.cooldown_s <= 60          # brief §8 range
    assert cfg.max_actions_in_flight == 1      # brief §6: one action at a time


def test_default_policy_rejects_the_disabled_io_tool():
    v = check(gate(), action(HOG, "set_container_io_weight", 100, 25))
    assert not v.approved and "allowed_action" in failed(v)
