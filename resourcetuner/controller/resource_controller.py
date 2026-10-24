"""Resource Controller + rollback storage (Step 7). The only code that changes the system.

It is the state-changing actuator behind the MCP tool layer: tools.py validates and resolves
the target, the Safety Gate approves, and only then ResourceController.apply() runs.

Guarantees
* Write-ahead log: an action is stored as "pending" (with its exact old value) BEFORE the
  system is touched, so a crash mid-apply still leaves enough to restore state.
* Verify after every change: the cgroup is read back; if it does not show the new value the
  old one is restored immediately and the action is "apply_failed".
* Rollback restores the stored old value (never "the opposite action") and verifies it.
* Idempotent: rolling back an already resolved action is a no-op that reports its status.
* A target that disappeared is recorded as "target_gone" — never an exception.

Knobs and how they are set (measured on this VM, Docker 29.1.3 / runc 1.4.0)
* cpu.weight  via --cpu-shares; runc maps shares -> weight non-linearly (256 -> 35,
              1024 -> 100), so we invert runc's formula; every weight 10..1000 is reachable.
* cpu.max     via --cpu-quota/--cpu-period (NOT --cpus: `--cpus 0` is silently ignored and
              leaves NanoCpus set); unlimited is restored with --cpu-quota -1.
* freeze      via docker pause / unpause.
* io.weight   not supported: Docker cannot restore it exactly and it has no effect here.

Action lifecycle (status column)
  pending -> applied -> kept | rolled_back | rollback_failed | target_gone
  pending -> apply_failed | target_gone
"in flight" = pending or applied (the Safety Gate allows only one at a time).
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from resourcetuner.mcp_server.tools import ProposedAction
from resourcetuner.telemetry.containers import find_cgroup_dir

log = logging.getLogger("resourcetuner.controller")

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "actions.db"
CPU_PERIOD_US = 100_000
IN_FLIGHT = ("pending", "applied")

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    action_id      TEXT PRIMARY KEY,
    tool           TEXT NOT NULL,
    target_id      TEXT NOT NULL,
    container_id   TEXT NOT NULL,
    container_name TEXT NOT NULL,
    knob           TEXT NOT NULL,
    old_value      TEXT NOT NULL,      -- JSON: exact value read before the change
    new_value      TEXT NOT NULL,      -- JSON
    status         TEXT NOT NULL,
    created_at     REAL NOT NULL,
    applied_at     REAL,
    resolved_at    REAL,
    detail         TEXT
);
CREATE INDEX IF NOT EXISTS actions_target ON actions(target_id, created_at);
CREATE INDEX IF NOT EXISTS actions_status ON actions(status);
"""


# --- cpu.weight <-> docker cpu-shares (runc / opencontainers-cgroups mapping) --------------

def weight_from_shares(shares: int) -> int:
    """runc's ConvertCPUSharesToCgroupV2Value (quadratic in log2; 2->1, 1024->100, 262144->10000)."""
    if shares <= 0:
        return 0
    if shares <= 2:
        return 1
    if shares >= 262144:
        return 10000
    l = math.log2(shares)
    return math.ceil(10 ** ((l * l + 125 * l) / 612.0 - 7.0 / 34.0))


def shares_for_weight(weight: int) -> int:
    """Smallest cpu-shares value that runc turns into exactly `weight` (monotonic -> bisect)."""
    if not 1 <= weight <= 10000:
        raise ValueError(f"cpu.weight out of range: {weight}")
    lo, hi = 2, 262144
    while lo < hi:
        mid = (lo + hi) // 2
        if weight_from_shares(mid) >= weight:
            hi = mid
        else:
            lo = mid + 1
    if weight_from_shares(lo) != weight:
        raise ValueError(f"cpu.weight {weight} is not reachable through docker cpu-shares")
    return lo


# --- system access, behind an interface so the controller logic is unit-testable -----------

class TargetGone(Exception):
    pass


class ContainerOps(Protocol):
    def set_knob(self, container_id: str, knob: str, value: Any) -> None: ...
    def read_knob(self, container_id: str, knob: str) -> Any: ...


class DockerOps:
    def __init__(self, client=None):
        import docker
        from docker.errors import NotFound
        self._client = client or docker.from_env(timeout=60)
        self._NotFound = NotFound

    def set_knob(self, container_id: str, knob: str, value: Any) -> None:
        api = self._client.api
        try:
            if knob == "cpu.weight":
                api.update_container(container_id, cpu_shares=shares_for_weight(int(value)))
            elif knob == "cpu.max":
                if value is None:
                    api.update_container(container_id, cpu_quota=-1)
                else:
                    api.update_container(container_id, cpu_quota=int(round(value * CPU_PERIOD_US)),
                                         cpu_period=CPU_PERIOD_US)
            elif knob == "freeze":
                state = self._state(container_id)
                if value == "paused" and state != "paused":
                    api.pause(container_id)
                elif value == "running" and state == "paused":
                    api.unpause(container_id)
            else:
                raise ValueError(f"unsupported knob {knob!r}")
        except self._NotFound as e:
            raise TargetGone(str(e)) from e

    def read_knob(self, container_id: str, knob: str) -> Any:
        if knob == "freeze":
            return "paused" if self._state(container_id) == "paused" else "running"
        cg = find_cgroup_dir(container_id)
        if cg is None:
            raise TargetGone(f"cgroup of {container_id[:12]} not found")
        try:
            if knob == "cpu.weight":
                return int((cg / "cpu.weight").read_text())
            if knob == "cpu.max":
                quota, period = (cg / "cpu.max").read_text().split()
                return None if quota == "max" else round(int(quota) / int(period), 2)
        except FileNotFoundError as e:
            raise TargetGone(str(e)) from e
        raise ValueError(f"unsupported knob {knob!r}")

    def _state(self, container_id: str) -> str:
        try:
            return self._client.api.inspect_container(container_id)["State"]["Status"]
        except self._NotFound as e:
            raise TargetGone(str(e)) from e


# --- action store (SQLite) ---------------------------------------------------------------

class ActionStore:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def insert_pending(self, a: ProposedAction) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO actions (action_id, tool, target_id, container_id, container_name, knob,"
                " old_value, new_value, status, created_at) VALUES (?,?,?,?,?,?,?,?,'pending',?)",
                (a.action_id, a.tool, a.target_id, a.container_id, a.container_name, a.knob,
                 json.dumps(a.old_value), json.dumps(a.new_value), time.time()))

    def set_status(self, action_id: str, status: str, detail: str | None = None, *, applied: bool = False,
                   resolved: bool = False) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "UPDATE actions SET status = ?, detail = COALESCE(?, detail),"
                " applied_at = CASE WHEN ? THEN ? ELSE applied_at END,"
                " resolved_at = CASE WHEN ? THEN ? ELSE resolved_at END WHERE action_id = ?",
                (status, detail, applied, now, resolved, now, action_id))

    def get(self, action_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["old_value"], d["new_value"] = json.loads(d["old_value"]), json.loads(d["new_value"])
        return d

    def in_flight(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT action_id FROM actions WHERE status IN (?, ?) ORDER BY created_at",
                                    IN_FLIGHT).fetchall()
        return [self.get(r["action_id"]) for r in rows]

    def last_action_at(self, target_id: str) -> float | None:
        """When the target was last actually changed (failed applies don't start a cooldown)."""
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(COALESCE(applied_at, created_at)) AS t FROM actions WHERE target_id = ?"
                " AND status NOT IN ('apply_failed')", (target_id,)).fetchone()
        return row["t"]

    def history(self, limit: int = 50) -> list[dict]:
        with self._lock:
            ids = [r["action_id"] for r in self._db.execute(
                "SELECT action_id FROM actions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]
        return [self.get(i) for i in ids]


# --- the controller ------------------------------------------------------------------------

class ResourceController:
    """State-changing Actuator for tools.ToolHandlers (requires a Safety Gate there)."""
    name = "docker"
    changes_system = True

    def __init__(self, store: ActionStore, ops: ContainerOps | None = None):
        self.store = store
        self.ops = ops or DockerOps()

    def apply(self, action: ProposedAction) -> dict:
        self.store.insert_pending(action)                       # write-ahead, old value first
        try:
            self.ops.set_knob(action.container_id, action.knob, action.new_value)
            observed = self.ops.read_knob(action.container_id, action.knob)
        except TargetGone as e:
            self.store.set_status(action.action_id, "target_gone", str(e), resolved=True)
            return {"status": "target_gone", "applied": False, "detail": str(e)}
        except Exception as e:
            restored = self._restore(action.container_id, action.knob, action.old_value)
            self.store.set_status(action.action_id, "apply_failed", f"{type(e).__name__}: {e}; {restored}",
                                  resolved=True)
            return {"status": "apply_failed", "applied": False, "detail": f"{e}; {restored}"}
        if observed != action.new_value:
            restored = self._restore(action.container_id, action.knob, action.old_value)
            detail = f"verify failed: wanted {action.new_value!r}, cgroup shows {observed!r}; {restored}"
            self.store.set_status(action.action_id, "apply_failed", detail, resolved=True)
            return {"status": "apply_failed", "applied": False, "detail": detail}
        self.store.set_status(action.action_id, "applied", f"verified {action.knob} = {observed!r}", applied=True)
        log.info("applied %s %s: %s -> %s", action.action_id, action.target_id, action.old_value, observed)
        return {"status": "applied", "applied": True, "observed": observed}

    def rollback(self, action_id: str) -> dict:
        row = self.store.get(action_id)
        if row is None:
            return {"status": "error", "detail": f"no action {action_id}"}
        if row["status"] not in IN_FLIGHT:
            return {"status": row["status"], "noop": True}       # idempotent
        try:
            self.ops.set_knob(row["container_id"], row["knob"], row["old_value"])
            observed = self.ops.read_knob(row["container_id"], row["knob"])
        except TargetGone as e:
            self.store.set_status(action_id, "target_gone", f"at rollback: {e}", resolved=True)
            return {"status": "target_gone", "detail": str(e)}
        except Exception as e:
            self.store.set_status(action_id, "rollback_failed", f"{type(e).__name__}: {e}", resolved=True)
            return {"status": "rollback_failed", "detail": str(e)}
        if observed != row["old_value"]:
            detail = f"verify failed: wanted {row['old_value']!r}, cgroup shows {observed!r}"
            self.store.set_status(action_id, "rollback_failed", detail, resolved=True)
            return {"status": "rollback_failed", "detail": detail}
        self.store.set_status(action_id, "rolled_back", f"verified {row['knob']} = {observed!r}", resolved=True)
        log.info("rolled back %s %s -> %s", action_id, row["target_id"], observed)
        return {"status": "rolled_back", "observed": observed}

    def keep(self, action_id: str) -> dict:
        """Evaluation decided the change helped (or was neutral): resolve it without rollback."""
        row = self.store.get(action_id)
        if row is None:
            return {"status": "error", "detail": f"no action {action_id}"}
        if row["status"] != "applied":
            return {"status": row["status"], "noop": True}
        self.store.set_status(action_id, "kept", resolved=True)
        return {"status": "kept"}

    def rollback_unresolved(self) -> list[dict]:
        """Crash recovery: restore every action still pending/applied."""
        return [{"action_id": r["action_id"], **self.rollback(r["action_id"])} for r in self.store.in_flight()]

    def _restore(self, container_id: str, knob: str, old_value: Any) -> str:
        try:
            self.ops.set_knob(container_id, knob, old_value)
            ok = self.ops.read_knob(container_id, knob) == old_value
            return "old value restored" if ok else "WARNING: old value could not be verified"
        except Exception as e:
            return f"WARNING: restore failed: {e}"


# --- live facts for the Safety Gate --------------------------------------------------------

class ControllerGateContext:
    """GateContext backed by the action store (history, in-flight) and the live classifier."""

    def __init__(self, store: ActionStore, classifier):
        self.store = store
        self.classifier = classifier

    def workload_of(self, container_name: str) -> str | None:
        return self.classifier.workload_of(container_name)

    def host_workload(self) -> str | None:
        return self.classifier.host_workload()

    def last_action_at(self, target_id: str) -> float | None:
        return self.store.last_action_at(target_id)

    def actions_in_flight(self) -> list[str]:
        return [r["target_id"] for r in self.store.in_flight()]
