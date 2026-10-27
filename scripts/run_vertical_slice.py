#!/usr/bin/env python3
"""Phase 2 Prototype Milestone: End-to-End Vertical Slice (§6, Step 9) — CLI.

The pipeline itself lives in `resourcetuner/pipeline/slice.py`, which the dashboard's run
manager drives too, so both paths run one implementation and record the same structured events.
This file only parses arguments, registers a run, and prints where things went.

Per cycle: throwaway warm-up build (discarded) → this cycle's before-measurement → wait for
real contention → ADK Supervisor → CrewAI diagnostic crew → ADK Decision Agent → Safety Gate →
Resource Controller (cgroup-verified) → after-measurement → Evaluation (±10% discrete delta,
rollback on DEGRADED/TIMEOUT) → Policy Store → Meta-Agent.

Usage:
  ./venv/bin/python scripts/run_vertical_slice.py --cycles 5 --scenario build_vs_indexer \
      --level high --baseline-duration 37.38
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")

from resourcetuner.events.store import EventStore, RunEventSink  # noqa: E402
from resourcetuner.pipeline import stages as S  # noqa: E402
from resourcetuner.pipeline.slice import DEFAULT_LOG_FILE, SliceConfig, VerticalSlice  # noqa: E402

log = logging.getLogger("resourcetuner.slice")


def setup_logging() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S", force=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ResourceTuner Phase 2 Milestone: Vertical Slice")
    p.add_argument("--cycles", type=int, default=5, help="Number of tuning cycles to run (default: 5)")
    p.add_argument("--scenario", default="build_vs_indexer", help="Workload scenario name (default: build_vs_indexer)")
    p.add_argument("--level", default="high", help="Workload intensity level (default: high)")
    p.add_argument("--baseline-duration", type=float, default=37.38,
                   help="Reference warm-cache baseline duration in seconds")
    p.add_argument("--cooldown-s", type=float, default=20.0, help="Cooldown seconds between cycles")
    p.add_argument("--log-file", type=Path, default=DEFAULT_LOG_FILE)
    p.add_argument("--no-start-scenario", action="store_true",
                   help="Assume scenario containers are already running")
    p.add_argument("--trigger-wait-timeout-s", type=float, default=600.0,
                   help="Give up if contention never appears (default: 600)")
    p.add_argument("--no-events", action="store_true",
                   help="Do not record structured events (the run will not be replayable)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()

    cfg = SliceConfig(
        cycles=args.cycles, scenario=args.scenario, level=args.level,
        baseline_duration=args.baseline_duration, cooldown_s=args.cooldown_s,
        log_file=args.log_file, start_scenario=not args.no_start_scenario,
        trigger_wait_timeout_s=args.trigger_wait_timeout_s,
    )

    sink, store, run_id = None, None, None
    if not args.no_events:
        store = EventStore()
        run_id = store.create_run("cli", scenario=cfg.scenario, level=cfg.level,
                                  cycles_requested=cfg.cycles, params=vars(args) | {"log_file": str(args.log_file)})
        store.update_run(run_id, status=S.RUN_RUNNING)
        sink = RunEventSink(store, run_id)
        log.info("Recording events as run %s (visible in the dashboard)", run_id)

    log.info("Starting ResourceTuner Vertical Slice Milestone (%d cycles)", cfg.cycles)
    log.info("Reference baseline benchmark: %.2fs | Cooldown: %.1fs", cfg.baseline_duration, cfg.cooldown_s)

    summary = asyncio.run(VerticalSlice(cfg, sink=sink).arun())

    if store and run_id:
        store.update_run(run_id, status=summary.get("status", S.RUN_COMPLETED),
                         cycles_done=summary.get("cycles_done", 0), error=summary.get("error"),
                         ended_at=__import__("time").time())

    log.info("Run finished: %s (%d/%d cycles). Log: %s",
             summary.get("status"), summary.get("cycles_done", 0), cfg.cycles, cfg.log_file)
    if summary.get("error"):
        log.error("Error: %s", summary["error"])
    print(json.dumps({k: summary.get(k) for k in ("status", "cycles_done", "error", "cleanup")}, indent=1,
                     default=str))
    return 0 if summary.get("status") == S.RUN_COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
