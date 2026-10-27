#!/usr/bin/env python3
"""Launches the dashboard app for Playwright, with its run/event store isolated to a temp
directory.

`resourcetuner.dashboard.main` always opens `EventStore()` at its hardcoded default path
(`data/events.db`) and has no flag to redirect it, so running `python -m resourcetuner.dashboard`
directly would make e2e runs read and write the project's real event store. `create_app()`
already accepts an injected `store`/`manager` for exactly this reason (it is how
`tests/test_dashboard_api.py` isolates its own fixtures) — this script is a thin Playwright-only
wrapper that uses that injection point. No file in `resourcetuner/` is modified.

The read-only legacy endpoints (`data/vertical_slice_log.jsonl`, `actions.db`, `policy.db`) are
left pointed at the real project data on purpose: they are read-only by construction
(`dashboard/history.py` imports nothing that can act), so letting the Historical Replay tests
read real recorded cycles is useful, not risky.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")

import uvicorn  # noqa: E402

from resourcetuner.dashboard.main import Sampler, create_app  # noqa: E402
from resourcetuner.events.store import EventStore  # noqa: E402
from resourcetuner.pipeline.runner import RunManager  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--data-dir", type=Path, required=True, help="isolated dir for events.db")
    args = ap.parse_args()

    args.data_dir.mkdir(parents=True, exist_ok=True)
    store = EventStore(args.data_dir / "events.db")
    manager = RunManager(store)

    sampler = Sampler()
    sampler.start()
    app = create_app(sampler, store=store, manager=manager)
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        sampler.stop()


if __name__ == "__main__":
    main()
