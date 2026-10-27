"""Canonical pipeline stage names and statuses (single source of truth for backend and UI).

The stage list covers both the brief's §3 pipeline and the Live Lab progress list: scenario
startup and warm-up/baseline belong to running an experiment, the rest to one tuning cycle.
"""

from __future__ import annotations

# Run-level stages (cycle is null)
SCENARIO_START = "scenario_start"
WARMUP = "warmup"
RUN_SUMMARY = "run_summary"

# Per-cycle stages, in pipeline order
BASELINE = "baseline"
TRIGGER = "trigger"
CLASSIFY = "classify"
SUPERVISOR = "supervisor"
DIAGNOSTICS = "diagnostics"
DECISION = "decision"
SAFETY_GATE = "safety_gate"
CONTROLLER = "controller"
POST_MEASURE = "post_measure"
EVALUATION = "evaluation"
POLICY = "policy"
META = "meta"
CYCLE_SUMMARY = "cycle_summary"

CYCLE_STAGES: tuple[str, ...] = (
    BASELINE, TRIGGER, CLASSIFY, SUPERVISOR, DIAGNOSTICS, DECISION, SAFETY_GATE,
    CONTROLLER, POST_MEASURE, EVALUATION, POLICY, META, CYCLE_SUMMARY,
)
RUN_STAGES: tuple[str, ...] = (SCENARIO_START, WARMUP, RUN_SUMMARY)
ALL_STAGES: tuple[str, ...] = RUN_STAGES + CYCLE_STAGES

# Statuses
WAITING = "waiting"
RUNNING = "running"
COMPLETED = "completed"
REJECTED = "rejected"
SKIPPED = "skipped"
FAILED = "failed"
EVIDENCE_UNAVAILABLE = "evidence_unavailable"   # only for historical records that never stored a stage

STATUSES: tuple[str, ...] = (WAITING, RUNNING, COMPLETED, REJECTED, SKIPPED, FAILED, EVIDENCE_UNAVAILABLE)
TERMINAL_STATUSES: tuple[str, ...] = (COMPLETED, REJECTED, SKIPPED, FAILED, EVIDENCE_UNAVAILABLE)

# Run statuses
RUN_REQUESTED = "requested"
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_ACTIVE_STATUSES: tuple[str, ...] = (RUN_REQUESTED, RUN_RUNNING)
RUN_TERMINAL_STATUSES: tuple[str, ...] = (RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED)
