"""Resource Controller tests (Step 7): apply -> confirm -> rollback -> confirm, idempotency,
target-gone handling, write-ahead store. Unit tests use fake ops; live tests use real Docker."""

import subprocess
import time
import uuid

import pytest

from resourcetuner.controller.resource_controller import (
    ActionStore, ControllerGateContext, DockerOps, ResourceController, TargetGone,
    shares_for_weight, weight_from_shares,
)
from resourcetuner.mcp_server.tools import ProposedAction, ToolHandlers
from resourcetuner.safety.gate import SafetyConfig, SafetyGate


def proposed(knob="cpu.weight", old=100, new=20, cid="c" * 64, name="rt-t-indexer", tool=None):
    tool = tool or {"cpu.weight": "set_container_cpu_weight", "cpu.max": "set_container_cpu_limit",
                    "freeze": "pause_container"}[knob]
    return ProposedAction(uuid.uuid4().hex[:12], tool, "container:" + name, cid, name, knob, old, new)


class FakeOps:
    """In-memory container knobs. `lie` makes read_knob report something else (verify failure)."""

    def __init__(self, **knobs):
        self.knobs = {"cpu.weight": 100, "cpu.max": None, "freeze": "running", **knobs}
        self.gone = False
        self.lie = None
        self.calls = []

    def set_knob(self, cid, knob, value):
        if self.gone:
            raise TargetGone("No such container")
        self.calls.append((knob, value))
        self.knobs[knob] = value

    def read_knob(self, cid, knob):
        if self.gone:
            raise TargetGone("No such container")
        return self.lie if self.lie is not None else self.knobs[knob]


@pytest.fixture
def store(tmp_path):
    return ActionStore(tmp_path / "actions.db")


# --- cpu.weight <-> cpu-shares ------------------------------------------------------------

def test_shares_mapping_matches_measured_runc_values():
    assert weight_from_shares(256) == 35 and weight_from_shares(1024) == 100   # measured on this VM
    assert weight_from_shares(2) == 1 and weight_from_shares(262144) == 10000


def test_every_policy_weight_is_exactly_reachable():
    for w in range(10, 1001):
        assert weight_from_shares(shares_for_weight(w)) == w


# --- controller logic with fake ops ---------------------------------------------------------

def test_apply_confirm_rollback_confirm(store):
    ops = FakeOps()
    c = ResourceController(store, ops)
    a = proposed(old=100, new=20)
    assert c.apply(a) == {"status": "applied", "applied": True, "observed": 20}
    assert ops.knobs["cpu.weight"] == 20 and store.get(a.action_id)["status"] == "applied"
    assert c.rollback(a.action_id)["status"] == "rolled_back"
    assert ops.knobs["cpu.weight"] == 100 and store.get(a.action_id)["status"] == "rolled_back"


def test_rollback_is_idempotent(store):
    ops = FakeOps()
    c = ResourceController(store, ops)
    a = proposed()
    c.apply(a)
    c.rollback(a.action_id)
    n_calls = len(ops.calls)
    assert c.rollback(a.action_id) == {"status": "rolled_back", "noop": True}
    assert len(ops.calls) == n_calls        # second rollback touched nothing


def test_verify_failure_restores_old_value(store):
    ops = FakeOps()
    ops.lie = 77                            # cgroup does not show what we asked for
    c = ResourceController(store, ops)
    a = proposed(old=100, new=20)
    r = c.apply(a)
    assert r["status"] == "apply_failed" and "verify failed" in r["detail"]
    assert ops.calls[-1] == ("cpu.weight", 100)          # old value written back
    assert store.get(a.action_id)["status"] == "apply_failed"
    assert store.in_flight() == []                       # a failed apply is not in flight


def test_target_gone_at_apply_and_at_rollback(store):
    ops = FakeOps()
    c = ResourceController(store, ops)
    ops.gone = True
    a = proposed()
    assert c.apply(a)["status"] == "target_gone"
    ops.gone = False
    b = proposed()
    c.apply(b)
    ops.gone = True                                      # container vanished before rollback
    r = c.rollback(b.action_id)                          # must not raise
    assert r["status"] == "target_gone" and store.get(b.action_id)["status"] == "target_gone"
    assert store.in_flight() == []


def test_write_ahead_record_exists_before_the_change(store):
    seen = []

    class Watching(FakeOps):
        def set_knob(self, cid, knob, value):
            seen.append(dict(store.get(action.action_id)))   # what is stored at the moment of change
            super().set_knob(cid, knob, value)

    action = proposed(old=100, new=20)
    ResourceController(store, Watching()).apply(action)
    assert seen[0]["status"] == "pending" and seen[0]["old_value"] == 100


def test_store_survives_restart_and_crash_recovery_rolls_back(tmp_path):
    path = tmp_path / "a.db"
    ops = FakeOps()
    a = proposed(knob="cpu.max", old=None, new=1.5)
    ResourceController(ActionStore(path), ops).apply(a)  # "crash" here: never resolved
    reopened = ActionStore(path)
    assert [r["action_id"] for r in reopened.in_flight()] == [a.action_id]
    out = ResourceController(reopened, ops).rollback_unresolved()
    assert out[0]["status"] == "rolled_back" and ops.knobs["cpu.max"] is None


def test_keep_resolves_without_touching(store):
    ops = FakeOps()
    c = ResourceController(store, ops)
    a = proposed()
    c.apply(a)
    n = len(ops.calls)
    assert c.keep(a.action_id) == {"status": "kept"} and len(ops.calls) == n
    assert store.in_flight() == [] and c.rollback(a.action_id)["noop"]


def test_gate_context_reads_history_from_store(store):
    class Live:
        def workload_of(self, name): return "CODE_INDEXING"
        def host_workload(self): return "BUILD"

    ctx = ControllerGateContext(store, Live())
    c = ResourceController(store, FakeOps())
    a = proposed()
    c.apply(a)
    assert ctx.actions_in_flight() == [a.target_id]
    assert abs(ctx.last_action_at(a.target_id) - time.time()) < 5
    c.keep(a.action_id)
    assert ctx.actions_in_flight() == []


def test_controller_requires_gate_in_tool_layer(store):
    with pytest.raises(ValueError, match="Safety Gate"):
        ToolHandlers(None, ResourceController(store, FakeOps()), gate=None)


# --- live Docker --------------------------------------------------------------------------

def _docker_ok():
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


docker_required = pytest.mark.skipif(not _docker_ok(), reason="docker daemon not available")


@pytest.fixture
def busy_container():
    name = f"rt-ctl-{uuid.uuid4().hex[:6]}"
    cid = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--label", "resourcetuner.managed=true",
                          "alpine:3.20", "sh", "-c", "while :; do :; done"],
                         check=True, capture_output=True, text=True).stdout.strip()
    yield name, cid
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _cpu_percent(cid, seconds=2.0):
    from resourcetuner.telemetry.containers import find_cgroup_dir
    stat = find_cgroup_dir(cid) / "cpu.stat"
    usage = lambda: int(stat.read_text().split()[1])  # noqa: E731
    u0, t0 = usage(), time.monotonic()
    time.sleep(seconds)
    return (usage() - u0) / 1e6 / (time.monotonic() - t0) * 100


@docker_required
def test_live_cpu_weight(busy_container, store):
    name, cid = busy_container
    ops = DockerOps()
    c = ResourceController(store, ops)
    a = proposed("cpu.weight", ops.read_knob(cid, "cpu.weight"), 20, cid, name)
    assert a.old_value == 100
    assert c.apply(a)["observed"] == 20 and ops.read_knob(cid, "cpu.weight") == 20
    assert c.rollback(a.action_id)["status"] == "rolled_back" and ops.read_knob(cid, "cpu.weight") == 100
    assert c.rollback(a.action_id)["noop"]


@docker_required
def test_live_cpu_limit_really_throttles_and_restores(busy_container, store):
    name, cid = busy_container
    ops = DockerOps()
    c = ResourceController(store, ops)
    assert ops.read_knob(cid, "cpu.max") is None
    a = proposed("cpu.max", None, 0.5, cid, name)
    assert c.apply(a)["status"] == "applied"
    assert 35 <= _cpu_percent(cid) <= 60                  # 0.5 core cap on a busy loop
    assert c.rollback(a.action_id)["status"] == "rolled_back"
    assert ops.read_knob(cid, "cpu.max") is None
    assert _cpu_percent(cid) > 80                         # unthrottled again
    hc = subprocess.run(["docker", "inspect", "-f", "{{.HostConfig.CpuQuota}}/{{.HostConfig.NanoCpus}}", name],
                        capture_output=True, text=True).stdout.strip()
    assert hc == "-1/0"                                   # docker config clean too


@docker_required
def test_live_pause_and_resume(busy_container, store):
    name, cid = busy_container
    ops = DockerOps()
    c = ResourceController(store, ops)
    a = proposed("freeze", "running", "paused", cid, name)
    assert c.apply(a)["status"] == "applied"
    assert _cpu_percent(cid) < 5                          # frozen: no progress
    assert c.rollback(a.action_id)["status"] == "rolled_back" and ops.read_knob(cid, "freeze") == "running"
    assert _cpu_percent(cid) > 80


@docker_required
def test_live_target_gone_at_rollback(busy_container, store):
    name, cid = busy_container
    c = ResourceController(store, DockerOps())
    a = proposed("cpu.weight", 100, 50, cid, name)
    c.apply(a)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    r = c.rollback(a.action_id)
    assert r["status"] == "target_gone" and store.in_flight() == []


@docker_required
def test_live_full_chain_tool_gate_controller(busy_container, store):
    """MCP handlers -> Safety Gate -> controller, with real containers and real cgroups."""
    from resourcetuner.mcp_server.tools import DockerTargetSource
    from resourcetuner.safety.gate import StaticContext
    name, cid = busy_container

    ctx = ControllerGateContext(store, StaticContext(workloads={name: "UNKNOWN"}, host="IDLE"))
    controller = ResourceController(store, DockerOps())
    h = ToolHandlers(DockerTargetSource(), controller, gate=SafetyGate(SafetyConfig.load(), ctx))
    tid = "container:" + name
    r1 = h.set_container_cpu_weight(tid, 50)
    assert r1["status"] == "applied" and r1["gate"] == "approved"
    r2 = h.set_container_cpu_limit(tid, 1.0)                # second action while first in flight
    assert r2["status"] == "rejected" and any(x.startswith("in_flight") for x in r2["reasons"])
    assert controller.rollback(r1["action_id"])["status"] == "rolled_back"
    r3 = h.set_container_cpu_limit(tid, 1.0)                # resolved, but cooldown still active
    assert r3["status"] == "rejected" and any(x.startswith("cooldown") for x in r3["reasons"])
    assert DockerOps().read_knob(cid, "cpu.weight") == 100
