"""Deterministic Safety Gate (brief §8). The ONLY component that can approve an action.

It runs inside the MCP server (resourcetuner/mcp_server/tools.py), between a tool call and
the actuator, so no agent can reach the OS without passing it. It never calls an LLM and
never trusts the proposer: every rule below is checked on every action, and a rejection lists
*every* rule that failed.

Checks
  allowed_action   the tool is known and changes exactly its own cgroup knob
  allowed_target   the target id is well-formed, running/paused, and not name-protected
  reversible       the exact old value is known, so rollback restores state
  bounds           new value within the policy bounds (tighter than the tool schema)
  jump_size        one action changes a weight by <= max_jump_factor, a limit by <= 1/cut_factor
  foreground       resources of protected workloads (BUILD/TESTING/DEV_SERVER, and whatever
                   the host is currently running) are never reduced; unknown class -> reject
  pause            pause only when allowed, and only for non-protected targets (via foreground)
  cooldown         no second action on the same target within cooldown_s
  in_flight        at most max_actions_in_flight unresolved actions system-wide (brief §6)
  degraded_source  action must not come from a cycle that ran on the local fallback model
                   (redundant with DegradedActionBlocker in the ADK layer; enforced here too
                   so the boundary holds regardless of which caller constructs the action)

Policy lives in config/safety.yaml. Runtime facts (workload classes, action history) come
from a GateContext: in tests a static one, live from the classifier and the Resource
Controller's action store (wired in Step 7).
"""

from __future__ import annotations

import fnmatch
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import yaml

from resourcetuner.mcp_server.tools import TARGET_PREFIX, ProposedAction, Target, Verdict

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "safety.yaml"
# Host workloads that make their own container protected. IDLE/UNKNOWN never protect anything:
# otherwise a lone CPU hog (classified UNKNOWN) could protect itself.
_ACTIVE_WORKLOADS = {"BUILD", "TESTING", "DEV_SERVER", "CODE_INDEXING"}


@dataclass
class SafetyConfig:
    allowed_actions: dict[str, str] = field(default_factory=lambda: {  # mirrors config/safety.yaml
        "set_container_cpu_weight": "cpu.weight",
        "set_container_cpu_limit": "cpu.max", "pause_container": "freeze"})
    protected_name_patterns: list[str] = field(default_factory=list)
    protected_workloads: list[str] = field(default_factory=lambda: ["BUILD", "TESTING", "DEV_SERVER"])
    bounds: dict = field(default_factory=lambda: {
        "cpu_weight": [10, 1000], "io_weight": [10, 1000], "cpu_limit_min_cores": 0.5})
    max_jump_factor: float = 5.0
    cpu_limit_max_cut_factor: float = 4.0
    cooldown_s: float = 45.0
    max_actions_in_flight: int = 1
    allow_pause: bool = True

    @classmethod
    def load(cls, path: str | os.PathLike = DEFAULT_CONFIG_PATH) -> "SafetyConfig":
        return cls(**(yaml.safe_load(Path(path).read_text()) or {}))


class GateContext(Protocol):
    def workload_of(self, container_name: str) -> str | None:
        """Classifier's class for this container (BUILD, ..., IDLE, UNKNOWN); None = no data."""
    def host_workload(self) -> str | None:
        """The host's current workload according to the classifier; None = no data."""
    def last_action_at(self, target_id: str) -> float | None:
        """Epoch seconds of the last action applied to this target, if any."""
    def actions_in_flight(self) -> list[str]:
        """Target ids with an applied action whose evaluation has not resolved yet."""


@dataclass
class StaticContext:
    """Hand-written facts, for unit tests and dry runs."""
    workloads: dict[str, str] = field(default_factory=dict)
    host: str | None = None
    last_actions: dict[str, float] = field(default_factory=dict)
    in_flight: list[str] = field(default_factory=list)

    def workload_of(self, container_name):
        return self.workloads.get(container_name)

    def host_workload(self):
        return self.host

    def last_action_at(self, target_id):
        return self.last_actions.get(target_id)

    def actions_in_flight(self):
        return list(self.in_flight)


def reduces_resources(action: ProposedAction) -> bool:
    """True if the action takes CPU/IO/progress away from its target."""
    old, new = action.old_value, action.new_value
    if action.knob in ("cpu.weight", "io.weight"):
        return old is None or new < old
    if action.knob == "cpu.max":
        return old is None or new < old       # None = unlimited, so any limit is a reduction
    return action.knob == "freeze"


class SafetyGate:
    def __init__(self, config: SafetyConfig, context: GateContext, clock: Callable[[], float] = time.time):
        self.cfg = config
        self.ctx = context
        self.clock = clock

    def evaluate(self, action: ProposedAction, target: Target) -> Verdict:
        results: list[tuple[str, str | None]] = []   # (check, failure reason or None)
        for name, check in (
            ("allowed_action", self._allowed_action), ("allowed_target", self._allowed_target),
            ("reversible", self._reversible), ("bounds", self._bounds), ("jump_size", self._jump_size),
            ("foreground", self._foreground), ("pause", self._pause), ("cooldown", self._cooldown),
            ("in_flight", self._in_flight), ("degraded_source", self._degraded_source),
        ):
            try:
                results.append((name, check(action, target)))
            except Exception as e:  # a crashing check must reject, never approve
                results.append((name, f"check error: {type(e).__name__}: {e}"))
        reasons = [f"{n}: {r}" for n, r in results if r]
        checks = [f"{n}: {'FAIL' if r else 'ok'}" for n, r in results]
        return Verdict(approved=not reasons, reasons=reasons, checks=checks)

    # --- individual checks: return None if OK, else a human-readable reason --------------

    def _allowed_action(self, a: ProposedAction, t: Target) -> str | None:
        knob = self.cfg.allowed_actions.get(a.tool)
        if knob is None:
            return f"tool {a.tool!r} is not an allowed action"
        if a.knob != knob:
            return f"{a.tool} may only change {knob}, not {a.knob}"
        return None

    def _allowed_target(self, a: ProposedAction, t: Target) -> str | None:
        if a.target_id != TARGET_PREFIX + t.name or a.target_id != t.target_id:
            return f"target id {a.target_id!r} does not match target {t.name!r}"
        if t.state not in ("running", "paused"):
            return f"target is {t.state}"
        for pat in self.cfg.protected_name_patterns:
            if fnmatch.fnmatch(t.name, pat):
                return f"{t.name} matches protected pattern {pat!r}"
        return None

    def _reversible(self, a: ProposedAction, t: Target) -> str | None:
        if not a.reversible:
            return "action is not reversible"
        if a.knob in ("cpu.weight", "io.weight") and not isinstance(a.old_value, int):
            return f"old {a.knob} unknown ({a.old_value!r}): rollback could not restore it"
        if a.knob == "freeze" and a.old_value not in ("running", "paused"):
            return f"old freeze state unknown ({a.old_value!r})"
        return None

    def _bounds(self, a: ProposedAction, t: Target) -> str | None:
        b = self.cfg.bounds
        if a.knob == "cpu.weight":
            lo, hi = b["cpu_weight"]
        elif a.knob == "io.weight":
            lo, hi = b["io_weight"]
        elif a.knob == "cpu.max":
            if a.new_value < b["cpu_limit_min_cores"]:
                return f"cpu limit {a.new_value} < policy minimum {b['cpu_limit_min_cores']} cores"
            return None
        else:
            return None
        if not (lo <= a.new_value <= hi):
            return f"{a.knob} {a.new_value} outside policy bounds [{lo}, {hi}]"
        return None

    def _jump_size(self, a: ProposedAction, t: Target) -> str | None:
        if a.knob in ("cpu.weight", "io.weight") and isinstance(a.old_value, int) and a.old_value > 0:
            factor = max(a.old_value, a.new_value) / min(a.old_value, a.new_value)
            if factor > self.cfg.max_jump_factor:
                return (f"{a.knob} {a.old_value} -> {a.new_value} is a {factor:.1f}x change "
                        f"(max {self.cfg.max_jump_factor:g}x per action)")
        if a.knob == "cpu.max" and a.old_value is not None and a.new_value < a.old_value:
            cut = a.old_value / a.new_value
            if cut > self.cfg.cpu_limit_max_cut_factor:
                return (f"cpu limit {a.old_value} -> {a.new_value} cuts {cut:.1f}x "
                        f"(max {self.cfg.cpu_limit_max_cut_factor:g}x per action)")
        return None

    def _foreground(self, a: ProposedAction, t: Target) -> str | None:
        if not reduces_resources(a):
            return None  # boosting a target is fine (bounds still cap it)
        cls = self.ctx.workload_of(t.name)
        if cls is None:
            return f"workload of {t.name} unknown to the gate: refusing to reduce its resources (fail closed)"
        if cls in self.cfg.protected_workloads:
            return f"{t.name} runs {cls}, a protected workload: its resources may not be reduced"
        host = self.ctx.host_workload()
        if host in _ACTIVE_WORKLOADS and cls == host:
            return f"{t.name} runs {cls}, the host's current workload: its resources may not be reduced"
        return None

    def _pause(self, a: ProposedAction, t: Target) -> str | None:
        if a.knob == "freeze" and not self.cfg.allow_pause:
            return "pause is disabled by policy"
        return None

    def _cooldown(self, a: ProposedAction, t: Target) -> str | None:
        last = self.ctx.last_action_at(a.target_id)
        if last is not None:
            elapsed = self.clock() - last
            if elapsed < self.cfg.cooldown_s:
                return f"{a.target_id} was acted on {elapsed:.0f} s ago (cooldown {self.cfg.cooldown_s:g} s)"
        return None

    def _in_flight(self, a: ProposedAction, t: Target) -> str | None:
        busy = self.ctx.actions_in_flight()
        if len(busy) >= self.cfg.max_actions_in_flight:
            return (f"{len(busy)} action(s) still in flight ({', '.join(busy)}); "
                    f"max {self.cfg.max_actions_in_flight} — evaluate before acting again")
        return None

    def _degraded_source(self, a: ProposedAction, t: Target) -> str | None:
        if a.source_degraded:
            return ("action came from a degraded LLM cycle (local fallback model); "
                    "no resource change is allowed from a degraded cycle")
        return None
