import asyncio
import json

import pytest

from resourcetuner.mcp_server import tools as T
from resourcetuner.mcp_server.tools import (
    DryRunActuator, Target, ToolHandlers, Verdict, build_server,
)


class FakeTargets:
    def __init__(self, *targets):
        self._targets = list(targets)

    def list(self):
        return list(self._targets)


def target(name="rt-build_vs_indexer-indexer-1", state="running", cpu_weight=100, io_weight=100, cpu_limit=None):
    return Target(target_id="container:" + name, container_id="c" * 64, name=name,
                  image="resourcetuner/indexer-sim", state=state, cpu_weight=cpu_weight,
                  io_weight=io_weight, cpu_limit_cores=cpu_limit, memory_max_bytes=None)


IDX = "container:rt-build_vs_indexer-indexer-1"


def handlers(*targets, gate=None):
    act = DryRunActuator()
    return ToolHandlers(FakeTargets(*(targets or (target(),))), act, gate=gate), act


# --- tool surface ------------------------------------------------------------------------

def _tools():
    h, _ = handlers()
    return {t.name: t for t in asyncio.run(build_server(h).list_tools())}


def test_tool_surface_is_exactly_the_minimal_set():
    names = set(_tools())
    assert names == set(T.ENABLED_TOOLS)
    assert "set_container_io_weight" not in names     # disabled in Step 7, never shown to agents
    assert not any(w in n for n in names for w in ("exec", "command", "shell", "kill", "restart", "memory", "nice"))


def test_schemas_carry_hard_bounds():
    tools = _tools()
    w = tools["set_container_cpu_weight"].inputSchema["properties"]["weight"]
    assert (w["minimum"], w["maximum"]) == (1, 10000)
    c = tools["set_container_cpu_limit"].inputSchema["properties"]["cpus"]
    assert c["minimum"] == 0.1 and c["maximum"] == float(T.HOST_CORES)
    assert tools["pause_container"].inputSchema["properties"]["target_id"]["pattern"].startswith("^container:")
    assert tools["list_targets"].annotations.readOnlyHint is True


def test_out_of_range_rejected_through_mcp_before_reaching_handler():
    h, act = handlers()
    srv = build_server(h)
    with pytest.raises(Exception):
        asyncio.run(srv.call_tool("set_container_cpu_weight", {"target_id": IDX, "weight": 0}))
    with pytest.raises(Exception):
        asyncio.run(srv.call_tool("set_container_cpu_limit", {"target_id": IDX, "cpus": 99}))
    assert act.actions == []


def test_mcp_call_returns_structured_result():
    h, act = handlers()
    out = asyncio.run(build_server(h).call_tool("set_container_cpu_weight", {"target_id": IDX, "weight": 20}))
    content = out[0] if isinstance(out, tuple) else out
    payload = json.loads(content[0].text)
    assert payload["status"] == "dry_run" and payload["old_value"] == 100 and payload["new_value"] == 20
    assert len(act.actions) == 1


# --- handler behaviour (called directly, as the brief asks for Step 5) ----------------------

def test_list_targets_hides_internals_and_labels():
    h, _ = handlers()
    t = h.list_targets()["targets"][0]
    assert t["target_id"] == IDX
    assert "container_id" not in t
    assert not any("label" in k or "workload" in k for k in t)


@pytest.mark.parametrize("bad", [
    "container:some-other-container",   # not managed -> not listed -> unnameable
    "rt-build_vs_indexer-indexer-1",    # missing prefix
    "1234",                             # a PID
    "host:systemd",
    None,
])
def test_unknown_or_protected_targets_are_rejected(bad):
    h, act = handlers()
    r = h.set_container_cpu_weight(bad, 50)
    assert r["status"] == "error" and r["error"] == "unknown_target"
    assert act.actions == []


@pytest.mark.parametrize("weight", [0, 10001, -5, 50.5, True, "50"])
def test_handler_revalidates_bounds(weight):
    h, act = handlers()
    assert h.set_container_cpu_weight(IDX, weight)["error"] == "invalid_argument"
    assert act.actions == []


def test_disabled_io_tool_errors_with_reason():
    h, act = handlers()
    r = h.set_container_io_weight(IDX, 50)
    assert r["error"] == "tool_disabled" and "no exact rollback" in r["message"]
    assert act.actions == []


@pytest.mark.parametrize("cpus", [0.0, 0.05, T.HOST_CORES + 1, "2", True])
def test_cpu_limit_bounds(cpus):
    h, _ = handlers()
    assert h.set_container_cpu_limit(IDX, cpus)["error"] == "invalid_argument"


def test_dry_run_records_exact_old_value_and_changes_nothing():
    h, act = handlers(target(cpu_weight=100, io_weight=2930, cpu_limit=None))
    r1 = h.set_container_cpu_weight(IDX, 25)
    r3 = h.set_container_cpu_limit(IDX, 1.5)
    r4 = h.pause_container(IDX)
    assert [r["status"] for r in (r1, r3, r4)] == ["dry_run"] * 3
    assert [(a.knob, a.old_value, a.new_value) for a in act.actions] == [
        ("cpu.weight", 100, 25), ("cpu.max", None, 1.5), ("freeze", "running", "paused")]
    assert all(a.reversible for a in act.actions)
    assert h.list_targets()["targets"][0]["cpu_weight"] == 100  # nothing actually changed


def test_no_change_is_not_recorded():
    h, act = handlers(target(cpu_weight=100, state="paused"))
    assert h.set_container_cpu_weight(IDX, 100)["status"] == "no_change"
    assert h.pause_container(IDX)["status"] == "no_change"
    assert act.actions == []


def test_exited_target_is_rejected():
    h, _ = handlers(target(state="exited"))
    assert h.pause_container(IDX)["error"] == "target_not_running"


def test_state_changing_actuator_requires_a_gate():
    class RealActuator:
        name, changes_system = "docker", True

        def apply(self, action):
            raise AssertionError("must never be reached in this test")

    with pytest.raises(ValueError, match="Safety Gate"):
        ToolHandlers(FakeTargets(target()), RealActuator(), gate=None)


def test_gate_rejection_stops_the_action_before_the_actuator():
    class DenyAll:
        def evaluate(self, action, target):
            return Verdict(False, ["cooldown: target acted on 12 s ago"])

    h, act = handlers(gate=DenyAll())
    r = h.set_container_cpu_weight(IDX, 10)
    assert r["status"] == "rejected" and r["reasons"] == ["cooldown: target acted on 12 s ago"]
    assert act.actions == []


def test_gate_sees_the_full_proposed_action():
    seen = []

    class Spy:
        def evaluate(self, action, target):
            seen.append((action.tool, action.knob, action.old_value, action.new_value, target.name))
            return Verdict(True, [])

    h, act = handlers(gate=Spy())
    assert h.set_container_cpu_limit(IDX, 2)["gate"] == "approved"
    assert seen == [("set_container_cpu_limit", "cpu.max", None, 2.0, "rt-build_vs_indexer-indexer-1")]
    assert len(act.actions) == 1
