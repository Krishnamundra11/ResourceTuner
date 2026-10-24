"""MCP tool layer: the only channel between agents and the OS (brief §3, §8).

Run as a stdio MCP server:

    ./venv/bin/python -m resourcetuner.mcp_server            # dry-run backend (Step 5)

What agents can do here, and nothing else:

    list_targets()                                   read-only
    set_container_cpu_weight(target_id, weight)      priority shift (cgroup cpu.weight)
    set_container_io_weight(target_id, weight)       priority shift (cgroup io.weight)
    set_container_cpu_limit(target_id, cpus)         throttle      (cgroup cpu.max)
    pause_container(target_id)                       pause         (cgroup freezer)

Safety properties that hold at the *interface* level, not in a prompt:

* No arbitrary command, process kill, restart, exec or memory-limit tool exists.
* Targets are opaque ids ("container:<name>") of containers labelled
  ``resourcetuner.managed=true``. Anything else (other containers, host processes, PIDs,
  the IDE, the desktop) cannot be named: it is not in ``list_targets`` and is re-checked and
  rejected as an unknown target on every call.
* Every argument has hard bounds in the tool's JSON schema, re-validated in the handler.
* Each action changes exactly one knob of one target and records the exact old value read
  from the target's cgroup at call time, so rollback restores state instead of guessing.
* Ground-truth ``workload=`` labels are never exposed to agents.
* Every action passes through a ``Gate`` (Step 6) inside this server, before the actuator, so
  an agent cannot reach the OS without it. A real (state-changing) actuator refuses to run
  without a gate. In Step 5 only the dry-run actuator exists: it records, never changes.

Process-level ``nice``/``ionice`` is deliberately absent: tested on this VM, raising a
container process's nice works, but restoring it fails with "Permission denied" (no
CAP_SYS_NICE), which would break exact rollback.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated, Any, Protocol

from pydantic import Field

from resourcetuner.telemetry.containers import find_cgroup_dir, parse_limits

log = logging.getLogger("resourcetuner.mcp")

MANAGED_LABEL = "resourcetuner.managed"
TARGET_PREFIX = "container:"
HOST_CORES = os.cpu_count() or 1

# Hard physical bounds (kernel/cgroup ranges). Policy bounds (how far, how often, which
# targets) belong to the Safety Gate, not to the schema.
CPU_WEIGHT_MIN, CPU_WEIGHT_MAX = 1, 10000      # cgroup v2 cpu.weight
IO_WEIGHT_MIN, IO_WEIGHT_MAX = 1, 10000        # cgroup v2 io.weight
CPU_LIMIT_MIN = 0.1                            # cores
CPU_LIMIT_MAX = float(HOST_CORES)

TOOL_NAMES = ("list_targets", "set_container_cpu_weight", "set_container_io_weight",
              "set_container_cpu_limit", "pause_container")

# Tools that exist in the design but are switched off on this machine, with the measured reason.
# A disabled tool is not registered with MCP (agents never see it) and errors if called directly.
DISABLED_TOOLS = {
    "set_container_io_weight": (
        "disabled (Step 7): no exact rollback via Docker (--blkio-weight 0 is ignored; nearest values "
        "give io.weight 102 or 91, not 100) and no effect on this VM (mq-deadline scheduler without "
        "io.cost: io.weight 1 vs 100 measured 85 vs 85 MiB/s)"),
}
ENABLED_TOOLS = tuple(n for n in TOOL_NAMES if n not in DISABLED_TOOLS)


# --- data model --------------------------------------------------------------------------

@dataclass
class Target:
    target_id: str
    container_id: str
    name: str
    image: str
    state: str                 # docker state: running | paused | ...
    cpu_weight: int | None
    io_weight: int | None
    cpu_limit_cores: float | None  # None = unlimited
    memory_max_bytes: int | None   # shown for context only; no tool changes memory

    def public(self) -> dict:
        """What agents may see (no container id internals, no ground-truth label)."""
        return {
            "target_id": self.target_id, "name": self.name, "image": self.image, "state": self.state,
            "cpu_weight": self.cpu_weight, "io_weight": self.io_weight,
            "cpu_limit_cores": self.cpu_limit_cores, "memory_max_bytes": self.memory_max_bytes,
        }


@dataclass
class ProposedAction:
    action_id: str
    tool: str
    target_id: str
    container_id: str
    container_name: str
    knob: str                  # "cpu.weight" | "io.weight" | "cpu.max" | "freeze"
    old_value: Any             # exact value read from the cgroup at call time
    new_value: Any
    reversible: bool = True
    requested_at: float = field(default_factory=time.time)
    # Set True when the LLM cycle that produced this action ran on the local fallback model (Ollama).
    # DegradedActionBlocker (ADK layer) already stops it before this point; the Safety Gate's
    # degraded_source check enforces the same rule at the MCP boundary independently.
    source_degraded: bool = False


@dataclass
class Verdict:
    approved: bool
    reasons: list[str]                                   # why it was rejected (every failed check)
    checks: list[str] = field(default_factory=list)      # every check that was evaluated, for audit


class Gate(Protocol):
    """Implemented by the deterministic Safety Gate in Step 6."""
    def evaluate(self, action: ProposedAction, target: Target) -> Verdict: ...


class Actuator(Protocol):
    name: str
    changes_system: bool       # False only for the dry-run actuator
    def apply(self, action: ProposedAction) -> dict: ...


class TargetSource(Protocol):
    def list(self) -> list[Target]: ...


# --- backends ----------------------------------------------------------------------------

class DryRunActuator:
    """Records actions and reports what *would* change. Never touches the system."""
    name = "dry-run"
    changes_system = False

    def __init__(self) -> None:
        self.actions: list[ProposedAction] = []

    def apply(self, action: ProposedAction) -> dict:
        self.actions.append(action)
        return {"status": "dry_run", "applied": False}


class DockerTargetSource:
    """Managed containers only, with their *enforced* limits read from cgroup v2."""

    def __init__(self, client=None, cgroup_root: Path | None = None):
        import docker  # imported lazily so unit tests need no docker daemon
        self._client = client or docker.from_env(timeout=60)
        self._cgroup_root = cgroup_root

    def list(self) -> list[Target]:
        rows = self._client.api.containers(filters={"label": f"{MANAGED_LABEL}=true"})
        out = []
        for r in rows:
            cid = r["Id"]
            name = r["Names"][0].lstrip("/")
            cg = find_cgroup_dir(cid, self._cgroup_root) if self._cgroup_root else find_cgroup_dir(cid)
            lim = parse_limits(cg) if cg else None
            out.append(Target(
                target_id=TARGET_PREFIX + name, container_id=cid, name=name, image=r.get("Image", ""),
                state=r.get("State", ""),
                cpu_weight=lim.cpu_weight if lim else None,
                io_weight=lim.io_weight if lim else None,
                cpu_limit_cores=lim.cpu_quota_cores if lim else None,
                memory_max_bytes=lim.memory_max_bytes if lim else None,
            ))
        return sorted(out, key=lambda t: t.target_id)


# --- tool logic (callable directly, independent of MCP) -----------------------------------

class ToolHandlers:
    def __init__(self, targets: TargetSource, actuator: Actuator, gate: Gate | None = None):
        if actuator.changes_system and gate is None:
            raise ValueError("refusing to start: a state-changing actuator requires a Safety Gate")
        self.targets = targets
        self.actuator = actuator
        self.gate = gate

    # read-only
    def list_targets(self) -> dict:
        return {"backend": self.actuator.name, "targets": [t.public() for t in self.targets.list()]}

    # actions
    def set_container_cpu_weight(self, target_id: str, weight: int, source_degraded: bool = False) -> dict:
        err = _check_int("weight", weight, CPU_WEIGHT_MIN, CPU_WEIGHT_MAX)
        return err or self._act("set_container_cpu_weight", target_id, "cpu.weight",
                                lambda t: t.cpu_weight, int(weight), source_degraded=source_degraded)

    def set_container_io_weight(self, target_id: str, weight: int, source_degraded: bool = False) -> dict:
        if "set_container_io_weight" in DISABLED_TOOLS:
            return _error("tool_disabled", DISABLED_TOOLS["set_container_io_weight"])
        err = _check_int("weight", weight, IO_WEIGHT_MIN, IO_WEIGHT_MAX)
        return err or self._act("set_container_io_weight", target_id, "io.weight",
                                lambda t: t.io_weight, int(weight), source_degraded=source_degraded)

    def set_container_cpu_limit(self, target_id: str, cpus: float, source_degraded: bool = False) -> dict:
        if isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or not (CPU_LIMIT_MIN <= cpus <= CPU_LIMIT_MAX):
            return _error("invalid_argument", f"cpus must be a number in [{CPU_LIMIT_MIN}, {CPU_LIMIT_MAX}]")
        return self._act("set_container_cpu_limit", target_id, "cpu.max",
                         lambda t: t.cpu_limit_cores, round(float(cpus), 2), source_degraded=source_degraded)

    def pause_container(self, target_id: str, source_degraded: bool = False) -> dict:
        return self._act("pause_container", target_id, "freeze",
                         lambda t: "paused" if t.state == "paused" else "running", "paused",
                         source_degraded=source_degraded)

    def _act(self, tool: str, target_id: str, knob: str, read_old, new_value, source_degraded: bool = False) -> dict:
        target = self._resolve(target_id)
        if target is None:
            # Same answer for "protected" and "does not exist": protected targets are unnameable.
            return _error("unknown_target", f"{target_id!r} is not a target; call list_targets")
        if target.state not in ("running", "paused"):
            return _error("target_not_running", f"{target_id} is {target.state}")
        old_value = read_old(target)
        if old_value == new_value:
            return {"status": "no_change", "target_id": target_id, "knob": knob, "value": old_value}
        action = ProposedAction(action_id=uuid.uuid4().hex[:12], tool=tool, target_id=target_id,
                                container_id=target.container_id, container_name=target.name,
                                knob=knob, old_value=old_value, new_value=new_value,
                                source_degraded=source_degraded)
        if self.gate is not None:
            verdict = self.gate.evaluate(action, target)
            if not verdict.approved:
                log.info("REJECTED %s %s: %s", tool, target_id, verdict.reasons)
                return {"status": "rejected", "action_id": action.action_id, "target_id": target_id,
                        "knob": knob, "old_value": old_value, "new_value": new_value,
                        "reasons": verdict.reasons}
            gate_note = "approved"
        else:
            gate_note = "no gate yet (Step 5, dry-run only)"
        result = self.actuator.apply(action)
        log.info("%s %s %s %s -> %s [%s]", result["status"], tool, target_id, old_value, new_value, gate_note)
        return {**result, "action_id": action.action_id, "target_id": target_id, "knob": knob,
                "old_value": old_value, "new_value": new_value, "gate": gate_note,
                "backend": self.actuator.name}

    def _resolve(self, target_id: Any) -> Target | None:
        if not isinstance(target_id, str) or not target_id.startswith(TARGET_PREFIX):
            return None
        return next((t for t in self.targets.list() if t.target_id == target_id), None)


def _check_int(name: str, value: Any, lo: int, hi: int) -> dict | None:
    if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
        return _error("invalid_argument", f"{name} must be an integer in [{lo}, {hi}]")
    return None


def _error(code: str, message: str) -> dict:
    return {"status": "error", "error": code, "message": message}


# --- MCP server ----------------------------------------------------------------------------

TargetId = Annotated[str, Field(pattern=r"^container:[A-Za-z0-9][A-Za-z0-9_.-]*$",
                                description="A target_id returned by list_targets.")]


def build_server(handlers: ToolHandlers):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    mcp = FastMCP(
        "resourcetuner",
        instructions=(
            "Resource controls for ResourceTuner. Only targets returned by list_targets exist. "
            "Each action changes one knob of one container and is reversible; a deterministic "
            "Safety Gate may reject it. There is no way to run commands."),
    )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def list_targets() -> dict:
        """List the containers that may be adjusted, with their current enforced limits
        (cpu_weight and io_weight default 100; cpu_limit_cores null = unlimited)."""
        return handlers.list_targets()

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def set_container_cpu_weight(
        target_id: TargetId,
        weight: Annotated[int, Field(ge=CPU_WEIGHT_MIN, le=CPU_WEIGHT_MAX,
                                     description="cgroup cpu.weight; default 100, lower = lower CPU priority")],
    ) -> dict:
        """Shift a container's CPU priority (relative share under contention). Reversible."""
        return handlers.set_container_cpu_weight(target_id, weight)

    if "set_container_io_weight" not in DISABLED_TOOLS:
        @mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True, openWorldHint=False))
        def set_container_io_weight(
            target_id: TargetId,
            weight: Annotated[int, Field(ge=IO_WEIGHT_MIN, le=IO_WEIGHT_MAX,
                                         description="cgroup io.weight; default 100, lower = lower disk priority")],
        ) -> dict:
            """Shift a container's disk I/O priority. Reversible."""
            return handlers.set_container_io_weight(target_id, weight)

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def set_container_cpu_limit(
        target_id: TargetId,
        cpus: Annotated[float, Field(ge=CPU_LIMIT_MIN, le=CPU_LIMIT_MAX,
                                     description=f"hard CPU cap in cores ({CPU_LIMIT_MIN}-{CPU_LIMIT_MAX})")],
    ) -> dict:
        """Throttle a container to at most `cpus` cores (cgroup cpu.max). Reversible."""
        return handlers.set_container_cpu_limit(target_id, cpus)

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def pause_container(target_id: TargetId) -> dict:
        """Freeze every process in a container until rolled back. The strongest action:
        the container makes no progress while paused."""
        return handlers.pause_container(target_id)

    return mcp


def main(argv: list[str] | None = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="ResourceTuner MCP tool server (stdio)")
    ap.add_argument("--backend", choices=["dry-run", "docker"], default="dry-run",
                    help="dry-run records only; docker really changes containers (always behind the Safety Gate)")
    ap.add_argument("--db", default=None, help="action store (default data/actions.db)")
    args = ap.parse_args(argv)
    # stdout carries the MCP protocol on stdio: all logging must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.backend == "dry-run":
        handlers = ToolHandlers(DockerTargetSource(), DryRunActuator(), gate=None)
    else:
        # Imported here: the controller and gate depend on this module's data types.
        from resourcetuner.classifier.live import LiveClassifier
        from resourcetuner.controller.resource_controller import ActionStore, ControllerGateContext, ResourceController
        from resourcetuner.safety.gate import SafetyConfig, SafetyGate
        store = ActionStore(args.db) if args.db else ActionStore()
        controller = ResourceController(store)
        live = LiveClassifier()
        live.start()
        gate = SafetyGate(SafetyConfig.load(), ControllerGateContext(store, live))
        handlers = ToolHandlers(DockerTargetSource(), controller, gate=gate)
    build_server(handlers).run("stdio")


if __name__ == "__main__":
    main()
